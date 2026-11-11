Updated: 2026-11-10. Source: the user's agent-reuse rules (pasted 2026-10-28), translated to English. Sections 0-11 are the user's rules; section 12 says where they are anchored, section 13 binds them to this repository. Models, effort, quota and review routing: [policy/PM_POLICY.md](policy/PM_POLICY.md).

# Agent orchestration and reuse rules (apply now, to every task from here on)

**Precedence:** section 13 (this repository's bindings) and [PM_POLICY](policy/PM_POLICY.md) override sections 0-11 where they differ: in this repository Claude workers are `claude -p` sessions through `cli_worker.py`, not the Agent tool, so read every mention of `SendMessage` or the Agent tool below as `cli_worker.py send`. Section 12 is already done (the anchors exist in AGENTS.md); at session start do section 2 only.

## 0. Why

Each time an old agent is dropped and a new one is created, the new agent reloads the whole system prompt, rules and context and rewrites the prompt cache from scratch. That costs far more usage than giving the next task to the old agent while its cache is still warm.

Goal: **prefer giving tasks to agents whose cache is still within its TTL**, create a new agent only when really needed, and clean up expired agents so they cost no context.

Scope:
- Every agent the orchestrator coordinates: Claude, Codex (CLI `codex`), Antigravity (CLI `agy`), any model.
- Dev agents and QA agents.
- At most **8 running agents at once** (all three platforms together). Idle agents waiting for work do not count. The total number of agents is unlimited.

## 1. Working mode

The current mode is the `CURRENT MODE` line in the `ORCHESTRATION` block of AGENTS.md (section 12; CLAUDE.md imports it). If it is missing, the default is **DELEGATE**.

- **DELEGATE (default):** delegating to agents is allowed; every rule in this file applies.
- **SOLO:** the orchestrator does every task itself, creates no agent and delegates nothing. A running agent may finish its current work; it gets nothing more and is cleaned up per section 4 when it expires.

Changing the mode:
- **Permanent:** only when the user says so clearly (for example "switch to SOLO", "do everything yourself from now on", "delegate to agents again", "back to DELEGATE"). Confirm in one sentence, update the `CURRENT MODE` line in **AGENTS.md** (CLAUDE.md imports it), tell the user. It applies at once in the current session.
- **One-off:** the user says "do this task yourself" or "delegate this task". Only that task; no file change.
- If it is unclear whether the change is permanent or one-off, ask one question before editing files.
- Only the user changes the mode; the orchestrator never does.

## 2. Start-up (once at the start of every orchestration session)

### 2.1 Check whether each CLI can set the prompt-cache TTL

Run real commands; never guess:
- Claude: `claude --version`. Look in `.claude/settings.json`, `~/.claude/settings.json` and the frontmatter of `.claude/agents/` for `subagentPromptCacheTtl`, `promptCacheTtl`, `experimental.cacheTtl`.
- Codex: `codex --version`, `codex --help`, `codex exec --help`, `codex exec resume --help`. Look at `~/.codex/config.toml`.
- Antigravity: `agy --version`, `agy --help`. Look at `~/.gemini/antigravity-cli/settings.json` .
- Filter output with `| grep -i -E "cache|ttl|retention|delete|remove|session|conversation"` to save tokens.

Rules:
- **If the CLI has a real cache-TTL option** (in `--help` output or official docs): set **dev = 1 hour, QA = 5 minutes**, like Claude.
- **If not:** set nothing. Use the **convention dev = 1 hour, QA = 5 minutes** and track it in the registry.
- **Never invent a setting or flag name.** If unsure, treat it as absent.
- Claude: keep the current configuration (dev 1h, QA 5 min). If the subscription is known to be on usage credits (over the plan limit), treat the Claude dev TTL as **5 minutes**, because the 1h frontmatter setting is then ignored.

### 2.2 Check the resume commands (to give a task to an old agent)

Confirm with `--help` that these still work:
- Claude subagent: `SendMessage` to the agent ID. Claude through the CLI: `claude -p --output-format json` to get `session_id`, then `claude -p --resume <session_id> "..."`.
- Codex: `codex exec --json "..."` to get the session/thread ID, then `codex exec resume <SESSION_ID> "..."`.
- Antigravity: `agy -p "..."` to get the conversation ID, then `agy -p "..." --conversation <ID>`.
- **Always resume by a specific ID.** Never `--last` or `--continue`; they can resume the wrong conversation.
- A platform that cannot resume has TTL = 0 (always a new agent there); note it.

### 2.3 Check how each CLI deletes a conversation or session

Look in `--help` for an official command that deletes a session or conversation by ID. If it exists, record it for section 4; otherwise use the path-based deletion in section 4.

### 2.4 Record the results in the registry

Write `platform_check` in the registry (section 3): version, whether a TTL setting exists, what was set, the resume command, the cleanup method, the check time. No re-check within the same session; a new session checks again in case a CLI was updated.

### 2.5 Clean up what the previous session left

Read the registry. Clean up expired agents at once per section 4. Delete every `pending_cleanup` entry that is now eligible.

## 3. Registry: `.agents/registry.json`

Create it if missing. Add `.agents/registry.json` and `.agents/log.md` to `.gitignore`. Do **not** ignore the whole `.agents/` folder, because `ORCHESTRATION.md` lives there. Only the orchestrator reads and writes the registry.

**The registry holds only live agents.** An expired agent is removed from the file completely (section 4); no list of retired agents is kept.

```json
{
  "platform_check": {
    "claude":      {"checked_at": 0, "version": "", "ttl_setting": "", "applied": {"dev": "1h", "qa": "5m"}, "resume": "SendMessage", "cleanup": "", "note": ""},
    "codex":       {"checked_at": 0, "version": "", "ttl_setting": null, "applied": "convention", "resume": "codex exec resume <id>", "cleanup": "", "note": ""},
    "antigravity": {"checked_at": 0, "version": "", "ttl_setting": null, "applied": "convention", "resume": "agy -p --conversation <id>", "cleanup": "", "note": ""}
  },
  "agents": [
    {
      "name": "dev-codex-01",
      "platform": "codex",
      "role": "dev",
      "model": "",
      "handle": "<agentId | session_id | conversation_id>",
      "kind": "subagent | cli",
      "worktree": "<worktree path or null>",
      "branch": "<branch or null>",
      "status": "busy | waiting_qa | ready",
      "ttl_sec": 3600,
      "last_active_at": 0,
      "current_task": "T-12",
      "area": "backend/auth"
    }
  ],
  "tasks": [
    {"id": "T-12", "type": "feature | bugfix | eval", "status": "queued | in_progress | waiting_qa | qa_pass | qa_fail | done", "dev": "dev-codex-01", "qa": "qa-claude-01", "qa_rounds": 0, "worktree": "", "branch": ""}
  ],
  "pending_cleanup": [
    {"worktree": "", "branch": "", "reason": "T-12 not merged", "since": 0}
  ]
}
```

Time rules:
- **Get the current time with `date +%s`.** Never estimate or guess it.
- `last_active_at` = the moment the agent **last returned a result**. Update it every time the agent finishes a turn (task, fix, evaluation). Each activity refreshes the cache, so expiry counts from the last activity, not from creation.
- `elapsed = now - last_active_at`.
- **Safety margin:** a dev agent is expired when `elapsed > ttl_sec - 60`; a QA agent when `elapsed > ttl_sec - 30`.
- Remove a task from `tasks` once it is `done` and its worktree is cleaned up, so the registry stays small.
- Log each decision as one line in `.agents/log.md`, for example `1730000000 ASSIGN T-13 -> dev-codex-01 (reuse, qa_pass, 900/3600s)`. Keep the last 300 lines. Read the log only when investigating.
- Read the registry only when a decision needs it.

## 4. Cleaning up finished agents (remove from the registry and delete leftovers)

**When.** An agent is cleaned up as soon as it is **finished and no further use is expected**: its task is accepted and integrated by the PM, no follow-up fix, review or related task is planned for it. Do not wait for the cache TTL. Also clean up expired agents (with the section 3 margins) and agents retired because their context is nearly full (section 9). **Never clean up a `busy` agent.** What is deleted, for every CLI (Claude Code, Codex, Antigravity): the registry entry, the agent's git worktree and branch, its scratch and junk files, and its conversation history. Keep only small reports its handoff cites (copy them into the task file or `tasks/archive/` first).

**Step 0: scratch and junk files.** Delete the agent's own trees under `.tmp/` (its task folder, `.tmp/codex-run/<name>`, `.tmp/agy-work/<workspace>` and `.tmp/agy-jobs` files of its job, test base temps, mutant copies, caches it made) and its `.cli-jobs` log folder, after the handoff has been read. Delete only paths this agent created (known from the brief, the registry or the worker name); never `models/`, `.venv*/`, `.secrets/`, `runs/` evidence, the data root or anything a running task uses; never recurse through a link.

In order:

**Step 1: remove from the registry.**
- Delete the agent from `agents` and log `<time> CLEANUP <name> (<reason>)`.
- Never call that agent again.
- If the agent has a `waiting_qa` or `qa_fail` task, the task stays in `tasks`. A later fix follows section 5 with a full handoff.

**Step 2: worktree and git branch** (only if the agent had its own worktree).
- **Delete only when every task on that worktree is `done` and its changes are merged or fully handed over.**
- Not yet eligible: put the worktree and branch in `pending_cleanup` with the reason; delete after the task is done.
- Eligible (the user has committed the integrated work, or the PM has discarded it, so nothing unrecorded is lost; a worktree copy of work that is already in the user's commit is discarded first, after checking that its diff is contained in that commit):
  - `git worktree remove <path>`, **never `--force`**;
  - then `git worktree prune`;
  - delete the branch with `git branch -d <branch>`, **never `-D`**.
- If git refuses (uncommitted changes or an unmerged branch): do not force; keep it in `pending_cleanup` and tell the user.
- **Never delete the repository's main worktree or the worktree the orchestrator is using.**

**Step 3: local conversation or session data of the CLI (the chat history with that agent).**
- Claude and Codex workers started through `cli_worker.py`: `cli_worker.py retire --name <worker> --purge-transcript` deletes the session by its exact UUID.
- With any other official delete command (section 2.3): use it with the exact ID.
- Without one:
  - Codex: delete exactly the session file containing `SESSION_ID` under `~/.codex/sessions/`.
  - Antigravity: delete exactly `~/.gemini/antigravity-cli/brain/<conversation-id>/` (authorised by the user's cleanup rule, AGENTS.md section 6, for conversations this orchestrator started).
  - Claude through the CLI (`claude -p`, its own session): delete exactly `<session_id>.jsonl` and its `<session_id>/` folder in the matching project folder under `~/.claude/projects/`.
  - Claude subagent (Agent tool, part of the orchestrator's session): **never delete by hand**; the transcript belongs to the running orchestrator session and Claude Code cleans it up by its retention period.
- Safe deletion:
  - find the path first, for example `find ~/.codex/sessions -name "*<ID>*"`;
  - delete only when the ID is non-empty and the path contains the **full** ID;
  - never use a wildcard wider than the ID;
  - never delete the orchestrator's own session.

**Step 4: server-side prompt cache.** It cannot and need not be deleted; it expires by itself. Do not try.

**Step 5:** log the cleanup result (what was deleted, what was kept and why).

When to clean up:
- At step 1 of every assignment (sections 5 and 7).
- At session start (section 2.5).
- Whenever a task becomes `done` and the agent has no further use (check `pending_cleanup`): delete its worktree, junk files and chat history at once.

## 5. Assigning a task to a DEV (every new task or bug-fix task)

**Step 1: clean up expired agents** per section 4.

**Step 2: filter dev candidates.** A candidate is within its TTL and meets **one** of:
- (A) `status = ready`: QA confirmed PASS on its previous task. Assign at once.
- (B) `status = waiting_qa` **and** `elapsed >= 2/3 x ttl_sec` (dev 1h: >= 2400 s): QA is not done but the agent has waited over 2/3 of its TTL. Give it a new task to keep the cache warm.
- A `waiting_qa` agent with `elapsed < 2/3 x ttl_sec` is **kept**, with no new task, so it is ready to fix bugs if QA finds any.

**Step 3: fit.** The task's difficulty and code area must suit the agent (model, area it holds). Use the difficulty/model criteria already agreed with the user; never change them. Drop unsuitable candidates.

**Step 4: choose among the remaining candidates**, in priority order:
1. The agent holding the same code area or one directly related to the task (least re-reading).
2. The agent with the larger `elapsed` (closer to expiry), to use its cache before it is lost; a newer agent stays usable later.

**Step 5: no candidate:**
- Fewer than 8 `busy` agents: create a new agent.
- 8 `busy` agents: queue the task (`queued`). Rerun this procedure whenever an agent finishes.

**Step 6: assign** with the template in section 8. Update the registry: agent `busy`, task `in_progress`.

## 6. Handling QA results

**QA PASS:** the task becomes `done`. A dev agent in `waiting_qa` becomes `ready`. Check `pending_cleanup` (section 4).

**QA FAIL:** create a bug-fix task (`type = bugfix`) with QA's finding list verbatim.
- The original dev is `waiting_qa` and within its TTL: **send the feedback to that same dev at once** (template 8.2).
- The original dev is `busy` with another task: **do not interrupt**. Queue the bug fix; when that dev finishes and is still within its TTL, give it the fix first, because it still holds the context.
- The original dev was cleaned up (expired): the bug fix follows section 5 like a normal task, with a **full handoff**: the original task description, the worktree/branch and changed files (or the diff), and the QA report.

## 7. Assigning work to QA

QA agents are reused like dev agents, with a 5-minute TTL.

When there is something to evaluate (a task just finished, or a retest after a fix):
1. Clean up expired QA agents per section 4 (30-second margin).
2. Find a QA agent that is `ready` and within its TTL (just finished an evaluation, or idle within its TTL).
3. Prefer giving a **retest to the QA agent that reviewed that task before**; then the oldest item in the queue.
4. No suitable QA agent: create one if fewer than 8 agents are `busy`, otherwise queue.
5. When a QA agent finishes and the evaluation queue still has work, give it the next item at once, before it expires.

## 8. Assignment message templates (always say clearly to the agent)

### 8.1 New task for an existing agent

```
[NEW TASK: T-13]
Previous task (T-12): <QA PASSED | QA is reviewing; any findings will be sent separately>.
T-13 is a NEW task, separate from T-12.
Goal: ...
Context you need: ...
Allowed scope: <files/modules>
Do not: change T-12's work again unless T-13 requires it (if so, give the reason in the report).
Done when: ...
Self-check before reporting: <test/lint/build commands>
Report in the section 8.4 format.
```

### 8.2 QA feedback to a dev

```
[QA FEEDBACK: T-12, round <n>] Verdict: FAIL.
Findings (verbatim from QA):
1. ... (reproduction steps / expected / actual / files)
Fix exactly these findings; do not widen the scope. Self-check, then report in the section 8.4 format.
```

### 8.3 Evaluation for an existing QA agent

```
[EVALUATE: T-13 | RETEST: T-12 round <n>]
Your previous work: <done>. This is a NEW evaluation.
Check: ... | PASS criteria: ... | (Retest: only confirm the findings from the previous round are fixed and nothing new broke in the related area.)
Report: PASS/FAIL + findings (reproduction steps, expected, actual, file, severity).
```

### 8.4 Dev report format (agents must use it)

```
Status: DONE | BLOCKED (reason)
Files changed: ...
Self-checked: <command + result>
Risks / notes for QA: ...
```

## 9. Keeping the cache when reusing (very important)

- **Never change the model, effort or tool set** of a reused agent; that loses the cache. A task that needs another model goes to another agent.
- When resuming, keep the same working directory and the same flags and configuration as at creation.
- Send only **what is new** (new task, new feedback). Never re-paste context the agent already has.
- An agent that reports a nearly full context, or that has been compacted several times, is retired after its current work (section 4); the next task goes to another agent.
- **Related tasks go to one agent.** Tasks in the same code area (same files, modules or feature) go to one dev agent as one batch, with one Step 0, instead of one agent each: it keeps the warm cache and keeps the parts consistent. Limits: the review is always done by a different, fresh agent; split the batch when the agent's context would pass about 150K tokens or the tasks touch unrelated code; a high-risk part gets its own commit inside the batch; independent batches still run in parallel within the busy-worker limit.
- Antigravity: `-p` mode may have a wait limit. Check `--help`; for long tasks raise the timeout so the agent is not cut off mid-task.

## 10. Checklist before every assignment (self-check, not printed)

- [ ] DELEGATE mode (or the user allowed delegation for this task)?
- [ ] Time taken with `date +%s` and `elapsed` computed for every relevant agent?
- [ ] Expired agents cleaned up per section 4 (registry, worktree, local session)?
- [ ] Old agents within their TTL preferred over a new agent?
- [ ] A `waiting_qa` agent under 2/3 of its TTL kept, with no new task?
- [ ] At most 8 running agents?
- [ ] The message says clearly "new task / feedback / retest" and the state of the previous work?
- [ ] Registry updated and one line written to `.agents/log.md`?

## 11. Reporting to the user

After each round of assignments, report in 1-3 lines:
- which task went to which agent;
- **reused or new** (with the reason);
- the number of running agents out of 8;
- which agents were cleaned up, and the reason for any worktree kept.

No long explanations.

## 12. Saving the rules

- **Single source:** the whole rule text lives in `.agents/ORCHESTRATION.md` (this file), with the update date on the first line; a new version overwrites it. Never copy it into AGENTS.md, CLAUDE.md, memory or any other file, and never `@import` it (that would load it into every agent).
- **Anchors:** AGENTS.md (the single rule file; CLAUDE.md imports it with `@AGENTS.md`, Codex and agy read it directly) holds only the two marker blocks `ORCHESTRATION` (orchestrator, with the `CURRENT MODE` line) and `AGENT-WORKER` (workers). Replace only the text between their `START`/`END` markers; never insert a duplicate.
- Never commit these files on your own; the user decides.
- After writing, report: files changed, the section 2 CLI results, the current mode and how to change it; a rule change takes effect after `/clear`, `/compact` or a restart.

## 13. Project bindings

- **Setup:** `.agents/project.json` binds the tooling to this repository; `python tools/agent-workflow/setup_project.py check` verifies CLIs, hooks, skills and bindings, `apply` repairs them (skill `agent-setup`). Run `check` at the start of an orchestration session, before section 2.
- **PM heartbeat:** while any agent works, `pm-supervisor.ps1 -MaxMinutes 45` runs under the Monitor tool and wakes the PM on every worker event and at least every 45 minutes ([PM_POLICY](policy/PM_POLICY.md) section 3); re-arm it after each wake.
- **Quota first:** all three CLIs run on the user's subscriptions with 5-hour and weekly limits; run `usage_snapshot.py` before each round of assignments and follow [PM_POLICY](policy/PM_POLICY.md) section 1. A provider whose quota is exhausted counts as unavailable for new agents until its window resets.
- **Claude workers** are CLI sessions started and resumed through `tools/agent-workflow/cli_worker.py` (`start`, `send --name`, `status`, `retire`); the registry `handle` is the worker name; not the Agent tool. Delete a retired worker's transcript with `cli_worker.py retire --name <worker> --purge-transcript` (exact UUID only); its `.tmp/cli-jobs` turn logs stay.
- **Codex workers:** `cli_worker.py start --backend codex` with a `codex-*` role from `codex_profiles.json`; Codex stops at Step 0 unless the brief says the plan is approved. Finished project `codex exec` sessions may be deleted by exact ID; never delete Codex app data.
- **Antigravity:** the orchestrator never reads the runner account's home and edits `~/.gemini` only to delete the exact conversation folder of an agy job it started (section 4); read-only jobs (reviews, pre-mortems, digests) run in parallel through `tools/agent-workflow/gemini-review.ps1`; jobs that write files run one at a time through `gemini-launch.ps1` and the `AgyRunner` (`scripts/agy-runner/`, Windows only); workspaces come from `gemini-ws.ps1 -Sanitize`. Every rule for a Claude worker binds an agy job too (brief, footer, Step 0, scope, report format, registry line, the 8-agent limit, no Git writes).
- **Approach scout:** before each non-trivial implementation a read-only scout (PM_POLICY section 2) writes a short approach note; the PM attaches it to the brief as advice, never as a decision; `brief_lint` refuses a feature or code brief without `Approach note:` (a path or `none: <reason>`).
- **Limit:** at most 8 running agents across the three CLIs; every test runs through `tools/agent-workflow/test_slot.py`.
- **No agent commits, pushes or merges** (AGENTS.md section 11): the PM brings accepted work into the main working tree uncommitted and the user commits. Files outside `.tmp` are deleted only after asking the user or a written analysis that they are garbage; never `models/`, `.venv*/`, `.secrets/` or `runs/` evidence.
- **PID identity:** a recorded worker PID counts as alive only with the same process creation time (`cli_worker.py status`).
- **GPU peers:** [policy/AGENT_PEER_RULES.md](policy/AGENT_PEER_RULES.md).
