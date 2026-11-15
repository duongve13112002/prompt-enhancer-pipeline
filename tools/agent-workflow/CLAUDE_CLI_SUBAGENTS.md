# Using Claude Code CLI sessions as subagents

For agents and developers who start, feed and measure Claude workers. Normative policy: [.agents/policy/PM_POLICY.md](../../.agents/policy/PM_POLICY.md) ("Claude Code CLI workers"), brief rules: [.agents/policy/AGENT_BRIEF_FOOTER.md](../../.agents/policy/AGENT_BRIEF_FOOTER.md). Tool reference and guard hooks: [README.md](README.md).
Commands run from the canonical checkout (the scripts derive the project root from their location). Measured values below are from 2026-10-10, Claude Code 2.1.296.

## 1. Why CLI sessions instead of the Agent tool

- **Decision 232:** every Claude implementer, reviewer, scout or checker is a persistent `claude -p` conversation (`--session-id`, then `--resume`). The PM talks to it turn by turn, like a user, and picks the effort per task.
- **Cache reuse:** the CLI writes a 1-hour prompt cache (Agent-tool subagents get 5 minutes). A resumed turn read about 17K cached tokens and wrote 0.6K; a new session rewrote about 14K.
- **No subagents inside workers:** the launcher withholds Agent, Task, Workflow, scheduling, cross-session, publishing and worktree tools, and denies `.secrets` and every git command that is not read-only (`hooks/tool_guard.py --git-mode worker`): workers never stage, commit, push or merge; the orchestrator stages in the worker's worktree to save the patch, then commits and pushes (AGENTS.md section 11, decision 77).
- **Decision 233, tier by difficulty:**

| Task | Implementer | Effort (changeable per `send`) |
|---|---|---|
| Easy, medium; scouts, checkers | Haiku 5.5 (never reviews) | `low`, `medium` |
| Moderately hard | Sonnet 5.5 | `medium`, `high` |
| Hard or high-risk | Opus 5.5 | `high`, `xhigh` |
| Opus fails repeatedly | Fable 5.1 (also the PM's advisor) | `xhigh`, `max` |

- A reviewer is never weaker than the implementer and never the same session. A small worker writes an edge-case list before each implementation.
- **Slots:** at most 4 busy CLI workers plus 1 Gemini job; test commands are limited separately by `test_slot.py` (footer rule 10).

## 2. Run a worker

```text
python -B tools/agent-workflow/cli_worker.py start --name <w> --model <alias> --effort <level> --cwd <dir> --message-file <file> --profile <role> [options]   # without --profile: --cache-ttl 1h|5m is required
python -B tools/agent-workflow/cli_worker.py send --name <w> --message-file <file> [--effort <level>] [--label <text>] [--timeout-minutes N]
python -B tools/agent-workflow/cli_worker.py status
python -B tools/agent-workflow/cli_worker.py notify --name <w> --message-file <file>
python -B tools/agent-workflow/cli_worker.py retire --name <w>
```

Run `start` and `send` in the background (the call blocks until the turn ends; default timeout 300 minutes, `--timeout-minutes`).

| `start` flag | Meaning |
|---|---|
| `--name` | Unique worker name; reusable only after `retire` |
| `--model` | Alias passed to `claude --model` (`haiku`, `sonnet`, `opus`; use a full model ID for others) |
| `--effort` | `low`, `medium`, `high`, `xhigh`, `max` |
| `--cwd` | Working directory; must be inside the project root (normally a worktree under `.tmp/worktrees/`) |
| `--message-file` | The brief (see below) |
| `--label` | Task label shown by `status` |
| `--timeout-minutes` | Per-turn limit, default 300; the launcher kills only that worker's process tree |
| `--profile <role>` | Normal path (decision 379): `dev-large`, `dev-medium`, `dev-small`, `review`, `spike`, `scout`, `helper` from [worker_profiles.json](worker_profiles.json) set window, nudge and TTL (values: [README](README.md#starting-workers)); an explicit flag overrides the profile value |
| `--cache-ttl 5m\|1h` | Profile value, else required (see section 3) |
| `--process-guard`, `--ctx-nudge`, `--loop-warning` | Guard hooks, on by default ([README](README.md#guard-hooks)); the nudge is the profile value, else 170K |
| `--autocompact-window N\|off` | Per-worker auto-compact window, profile value, else 230000 so compaction fires near 200K ([README](README.md#context-policy)); fixed for the worker's life |
| `--mcp default\|minimal` | `minimal` removes all MCP servers (about 6% smaller first call; also removes the research connectors) |

- **Message = brief + footer:** the file contains the task brief with its Configuration impact, then [.agents/policy/AGENT_BRIEF_FOOTER.md](../../.agents/policy/AGENT_BRIEF_FOOTER.md) verbatim. Write it as UTF-8 without BOM (a BOM is tolerated but pollutes the prompt). Put long evidence in files and reference them.
- **Results:** `.tmp/cli-jobs/<w>/NNN.result.md` (final reply, or the exit code if there was none) and the full `NNN.jsonl` stream log. `start` prints the result path and `context=<tokens> cost=<usd>`.
- **Continue, do not restart:** send fix rounds, delta re-reviews and follow-ups in the same area to the same worker. `status` shows state, idle minutes, context size and turns. A `busy` worker cannot take `send`; use `notify`. It queues the message in `.tmp/pm-state/inbox/<session>.md`, and the PostToolUse hook delivers it once as `[pm-message]` at the next tool call. A later message is appended to an undelivered one. Workers without the PostToolUse hook are refused.
- **Retire** when the next task is unrelated or the worker was idle for 60 minutes (the cache is gone). A fresh worker with a short handoff is cheaper than a stale resume. Do not retire a worker mid-task for context size: re-reading costs 2.5-5 times more than letting it compact near 200K (`.tmp/context-policy/data-report.md`). Never delete a reusable session, log or workspace before the task chain is accepted.
- The hook appends `[ctx-nudge]` at 170K: write progress, decisions, open findings and the next step into the task file then; auto-compaction follows near 200K and keeps only a summary.

## 3. Prompt caching

- **How it works:** each call re-sends the whole context; unchanged prefix tokens are billed as cache reads (about 0.1x), new or changed tokens as cache writes (about 2x for the 1h TTL, about 1.25x for 5m) or plain input (1x). A resumed session hits only while the prefix is byte-identical and the TTL has not expired since the last call.
- **Choosing the TTL:** `1h` pays for multi-turn chains and turns with tool runs longer than 5 minutes. `5m` is right for one-shot jobs with no follow-up. `send` prints a WARNING when the idle time exceeds the TTL (resume would rewrite the whole context): start a fresh worker instead. Never send keep-warm pings.
- **What a miss looks like:** in `.tmp/cli-jobs/<w>/NNN.jsonl` each `assistant` event has `message.usage` with `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` (one API call can repeat under the same `message.id`; count it once). The first call of a session is an expected write. A later call is a miss when `cache_read` is more than 4096 tokens below the previous call's context (input + read + write): the prefix was rewritten, so `cache_creation` is large instead. `send` prints `resume cache: first call read X of previous Y`; X far below Y is a resume miss.

```text
python -B tools/agent-workflow/cache_check.py [--since-hours 24] [--name <w>]
```

It prints hit rate per log, a total, and each miss (worker, log, "turn start (resume)" or call number, tokens rewritten, minutes after the previous turn); for Gemini jobs it prints `cache_read_tokens` against uncached `input_tokens`. `workflow_metrics.py` counts the same misses per worker (`Resume miss`, `Call miss`, `Miss write`).

- **Known causes and fixes** (baseline 97.3% hit rate; probe after the fixes read 44,175 of 44,177 tokens on resume):

| Cause | Fix or rule |
|---|---|
| Resume 15-30 minutes after a turn although the TTL was 1h: the prefix itself changed (git status snapshot, auto memory) | `cli_worker.py` sets `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` (for `1h`), `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and passes `--exclude-dynamic-system-prompt-sections` |
| Different flags, tools, cwd or effort between turns | Keep them identical; `send --effort` changes the request, so check the `resume cache:` line after changing it |
| Idle longer than the TTL | Retire and restart with a handoff (section 2) |
| Mid-turn drop to about 12K tokens | Autocompaction (near 200K for workers started with the default window); expected, not a defect |
| Image-heavy Haiku sessions | Media is stripped from context and the prefix changes; expect misses |

- **Gemini (agy):** the result JSON (read as `utf-8-sig`) has cumulative `usage` per conversation; the real per-turn context is the delta of `input_tokens` plus `cache_read_tokens` between two results. Continue a conversation only within about 20 minutes (a 24-minute follow-up read nothing from cache) and below about 400K context; otherwise save a short summary and start a new conversation. Print-mode `/compact` does not compact.

## 4. Usage

- **Input-equivalent tokens** = uncached input x1 + cache read x0.1 + cache write x2 (1h price); output is listed separately. Cost in USD comes from the result event (`total_cost_usd`, cumulative per session).
- **Per worker or task:**

```text
python -B tools/agent-workflow/workflow_metrics.py --last 10 [--since TS] [--until TS] [--guarded yes|no] [--split TS] [--out FILE]
python -B tools/agent-workflow/usage_breakdown.py <PM-transcript.jsonl> --date YYYY-MM-DD
```

`workflow_metrics.py` gives one row per worker: first-call tokens, input-equivalent total, cost, cache misses, hook blocks, nudges and loop warnings, turns and calls. `usage_breakdown.py` totals all workers by model against one PM session transcript for a day. Compare groups of at least 10 workers of the same model, effort and task kind; use medians ([README](README.md#evaluation-method)).
- **Where the usage goes (2026-10-10):** the PM was about a quarter of the input-equivalents (954 calls at about 250K context: every PM tool call costs like a large worker call); Opus and Sonnet workers about a third each; Haiku about 4%. Levers: batch PM tool calls, read reviews by extract, delegate mechanical multi-call work to a small-context worker, retire near 150K, give Gemini reviews first (no Claude quota).
- **Plan usage (PM only):** the desktop app's usage tool (`get_usage`, session tool `mcp__ccd_session_mgmt__get_usage`) reports the 5-hour and weekly percentages. The PM checks it on wakes and paces workers to it. Workers cannot call it.
- **Pacing rules:**
  - Decision 243: when Gemini or Codex is out of quota, one fresh Claude reviewer is enough; never hold work for the reset; run the other family later as a supplementary review.
  - Decision 244: autocompact of the PM session stays at 400K (workers use the per-worker window above); PM replies to the user are short; worker replies are at most about 10 lines (footer rule 11); a heartbeat wake with no event is one combined status command.
  - Waiting workers cost nothing; each PM wake costs a full-context cache read, so keep the supervisor armed at 50 minutes or less to keep the PM's own cache alive.

## 5. Gemini jobs through `agy`

Normative rules: PM_POLICY "Gemini through Antigravity" and `.agents/ORCHESTRATION.md` section 13: every Claude worker rule binds an agy job too (decision 581). One `AgyRunner` job at a time (it writes files and runs commands); read-only jobs overlap, each counts as a running agent.

```text
powershell -File tools/agent-workflow/gemini-ws.ps1 -Worktree <wt> -Base <sha> -Head <sha> -TaskFile <repo path> -Workspace <name>
powershell -File tools/agent-workflow/gemini-review.ps1 -Workspace <name> -PromptPath <file under root> -JobId <id> [-TimeoutMinutes 20] [-LintWaive <reason>]
powershell -File tools/agent-workflow/gemini-launch.ps1 -Workspace <name> -PromptPath <file under root> -JobId <id> [-TimeoutMinutes 45] [-Conversation <uuid>] [-LintWaive <reason>]
```

- `gemini-review.ps1` (reviews, pre-mortems, digests): user account, default permission policy (reads inside the workspace only, no writes, no shell), one background call per job, the result is the reply in `_results/<id>.json`; exits 2 `INCOMPLETE` on a bad status, an empty reply or a denial. Never add `--dangerously-skip-permissions`.
- Both launchers run `brief_lint.py` on `notes/brief.md` unless `-LintWaive` names why not.

- `gemini-ws.ps1` builds `.tmp/agy-work/<name>` from a committed head with `git archive` (allowlisted directories `src`, `tests`, `configs`, `scripts`, `tools`, `docs`, `tasks`, `contracts`, `prompts`: no ignored files, no `.secrets`) plus `notes/` (task file, `delta.diff`, `stat.txt`); it fails if the workspace exists. `-Workspace` must be one plain folder name (letters, digits, `.`, `_`, `-`; no separator, space, leading dot or trailing dot). Each build writes only into its own staging folder `.tmp/agy-work/.build-<name>-<guid>` and publishes with one atomic move, so of concurrent builds of one name exactly one wins, and a failed build leaves no workspace. `-Sanitize` rewrites `notes/*.md` so the runner path lint passes.
- `gemini-launch.ps1` validates the prompt before calling `scripts/agy-runner/invoke-job.ps1`: no double quotes, exactly one line, workspace present, and no earlier result for the JobId. **A JobId is not reusable** (results are `.tmp/agy-work/_results/<id>.{json,done,stderr.txt}`); use a new id per job. `-Conversation` continues an earlier conversation (cache reuse).
- Prompts name the exact workspace files and say: no web, URL or network tool, no subagents, run no commands, finish in one response. An empty `response` with `denied_actions` means a tool was denied; the status can still read SUCCESS.
- Gemini changes are diffed by the PM before use. Failures from environment limits (for example Windows handle tests under the restricted account) are reported, never patched around.
- `pm-supervisor.ps1` watches the job registry (`.tmp/pm-state/agents.json`) and reports `completed`, `stalled`, `overtime` and `empty_response`.

## 6. Safety rules

- **Footer rule 12:** stop only processes you started, by the PID you recorded; never by name, image or command-line match. The process-guard hook blocks name- or pattern-based stops for workers; it is a safety net, not a sandbox (limits in the [README](README.md#guard-hooks)).
- Never `--dangerously-skip-permissions` or `bypassPermissions`; the launcher uses `--permission-mode dontAsk` with an explicit allowlist and deny rules.
- Never open, list, copy or print `.secrets/`; the launcher denies it. Do not put credentials, personal data or private images in briefs, logs or notes.
- Everything stays under the project root: worktrees under `.tmp/worktrees/`, scratch under `.tmp/`; `--cwd` outside the root is refused. Temp files are removed before handoff (decision 109).
- Workers leave their change unstaged in the worktree and never commit, push or merge; the orchestrator saves the patch, commits and pushes (decision 77). Stop every process you start before replying (footer rule 7).
- The host's `ANTHROPIC_*` variables are removed from the worker environment; the CLI uses its own login.
