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
- **Base:** commit d5bc788, which accepts T-024 and holds the accepted T-025; working folder `.tmp/worktrees/T-026`.

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
- **Step 0 approval (decision 86):** while the user is away the PM approves the Step 0 itself if the plan changes no permission or allow rule and spends nothing beyond normal worker turns; a plan that does either waits for the user.
- **Pre-approved by the user (decision 93):** the allow rules of item 6, each naming one command with no wildcard over arguments that could write (`usage_snapshot.py`, `cli_worker.py status`, `cli_worker.py send`, `purge_tmp.py`), and keep-awake through `SetThreadExecutionState` only while work is in flight, with no power-plan or system-setting change. With those, the PM approves the Step 0; any other allow rule or setting still goes to the user.
- **Order:** starts after T-024 and T-025 are accepted.
- **Brief assembly:** `.agents/policy/cli/codex.md`, this file, `.agents/policy/roles/implementer.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` into `.tmp/T-026/brief.md`; lint it.

## Step 0 (phase 1: items 1-4 and 8; Claude Opus 5.5 implementer, 2026-11-15)

### Research evidence gathered for Step 0 (R-021)

| Question | Source | Finding | Class |
|---|---|---|---|
| Claude status-line fields | https://code.claude.com/docs/en/statusline | "`rate_limits.five_hour.resets_at`, `rate_limits.seven_day.resets_at` \| Unix epoch seconds when the 5-hour or 7-day rate limit window resets"; "`used_percentage` ... from 0 to 100"; "`rate_limits`: appears only for claude.ai Pro, Max, and Ultra subscribers, or behind a Claude apps gateway that sets a spend limit for you. Even then, it appears only after the first API response in the session. Each window (`five_hour`, `seven_day`, `spend_limit`) may be independently absent, and Claude Code drops a window once its `resets_at` time passes." New since N135: `spend_limit` window. | documented |
| How the status line runs | same page | "Claude Code debounces updates at 300ms ... If a new update triggers while your script is still running, Claude Code cancels the in-flight script."; re-runs when "A rate-limit window ... reaches its `resets_at` time"; "On Windows, Claude Code runs status line commands through Git Bash when Git Bash is installed, or through PowerShell when Git Bash is absent." Registered by `statusLine: {type: "command", command: ...}` in user or project settings. | documented |
| Does Claude Desktop run the status line | statusline page; https://code.claude.com/docs/en/desktop | Neither page says so; the Desktop page shows only "the usage ring ... Click it to see your plan usage". | unknown (treated as: Desktop does not run it) |
| `rate_limit_event` | https://code.claude.com/docs/en/agent-sdk/typescript.md (`SDKRateLimitEvent`) | `status: "allowed" \| "allowed_warning" \| "rejected"; resetsAt?: number; utilization?: number`; units not stated. | documented |
| Its units, real data | `.tmp/cli-jobs/t026-opus-dev/001.jsonl:10` (main checkout) | `"resetsAt":1795010400,"rateLimitType":"seven_day",...,"unifiedWindows":{"seven_day":{"utilization":0.15,...}}`; 1795010400 = 2026-11-18T14:00:00Z, the same weekly reset the PM copied from Desktop `get_usage` into `.tmp/pm-state/quota-blocks.json`. So `resetsAt` is epoch seconds and `utilization` is a fraction 0..1. All 65 events in 42 worker logs carry only `seven_day`. `rateLimitType` and `unifiedWindows` are undocumented. | experiment |
| Structured `/usage` | SDK page, `SDKUsageReport` | "When you send `/usage` as a prompt, Claude Code delivers the report as an assistant message ... It attaches `usage_report`" with `rate_limits.limits[]`: `kind` (`session`, `weekly_all`, `weekly_scoped`), `percent` "0-100", `resets_at` "ISO 8601 timestamp". | documented |
| Antigravity status-line `quota` | https://antigravity.google/docs/cli/statusline/ | "Maps quota bucket IDs to their status: `remaining_fraction` or `remaining_amount`, `reset_time`, `reset_in_seconds`, and `disabled` (optional)."; example bucket `gemini-weekly`, `reset_time` "2026-07-06T07:50:32Z"; registered in "`~/.gemini/antigravity-cli/settings.json`" (outside the project); run with `sh -c` (`cmd /c` on Windows). When `quota` is absent: not stated. | documented |
| `agy -p /usage` | https://antigravity.google/docs/cli/headless/ ; .../commands/usage/ | Slash commands "produce a text report rather than a standard event stream"; whether `--output-format json` wraps it: not stated. The PM's 2026-11-13 record (`quota-blocks.json`) shows a JSON reply holding a tab-separated text line `Gemini Models\tWeekly Limit Remaining\t0%\t2026-11-13T23:54:06Z`. | documented + experiment (PM) |
| Desktop scheduled sessions and settings (phase 2 lead) | https://code.claude.com/docs/en/desktop-scheduled-tasks | "Allow rules from `~/.claude/settings.json` also apply to scheduled task sessions."; project settings are not named; "To prevent idle-sleep, turn on **Keep computer awake** in **Settings > Desktop app**"; History shows skipped runs and why. | documented |
| Versions on the dev machine | `claude --version`, `agy --version` | `2.1.325 (Claude Code)`, `1.5.9` | experiment |

### 1. Unknowns and failure modes

- The status line runs on every redraw and is cancelled mid-run: the script must be fast, write atomically (temp sibling, `os.replace` with a short retry on Windows `PermissionError`), never raise, and always print one line and exit 0.
- Desktop probably never runs the status line, so the PM (in Desktop) gets no Claude file from it; worker logs give only the weekly window; the 5-hour window then stays `unknown` unless the terminal status line or a `/usage` probe supplies it.
- `agy -p` workers likely do not run the Antigravity status line (headless), and its registration lives in the user's home, so the Antigravity file appears only after the user registers it and runs `agy` interactively.
- The `agy /usage` JSON holds a text table: reading "remaining" versus "used" from its row labels would be a word rule (AGENTS.md section 5). Only structural columns (an ISO time, a percent) can be taken without labels.
- `quota-blocks.json` already holds PM-written keys (`waiting_tasks`, `near_limit`, `wake_*`); a writer that replaces the file loses the wake record.
- `collect()` is called by `cli_worker.py` for the per-turn ledger (`cli_worker.py:2035`, `2117`): it must stay free of side effects.
- A missing source file must stay `unknown`, never "unlimited": only a present, fresh source that reports no limit is unlimited.

### 2. Questions (ranked, with proposed answers)

1. **Two one-off probes:** may I run once each, from the worktree, `agy -p /usage --output-format json` and `claude -p /usage --output-format stream-json --verbose` (local slash commands, no model turn per the docs above, but a network call)? Outputs go to `.tmp/T-026/probes/`. *Proposed: yes.* Without them the Antigravity command output is passed through untouched and item 8 relies on worker events only.
2. **Antigravity status line registration** is in `~/.gemini/antigravity-cli/settings.json`, outside the project. *Proposed:* the one script accepts both payloads; I document the snippet (README, N135) and the user or PM registers it; I touch no home file. Antigravity then reads the fresher of its status-line file and the configured `antigravity_command`.
3. **Claude source inside Desktop (item 8).** *Proposed:* per window, the newest observation among (a) the status-line file (terminal CLI) and (b) the newest `rate_limit_event` across the newest worker turn logs `.tmp/cli-jobs/*/*.jsonl` (bounded by the existing `usage.session_file_limit`), with `utilization × 100` per the experiment and the window named by `rateLimitType`. `--claude-file` stays as an explicit override (current behaviour). The `/usage` `usage_report` becomes a source only after probe 1 confirms it costs no model turn (then a follow-up, not this phase).
4. **`quota-blocks.json` ownership.** *Proposed:* `usage_snapshot.py main()` rewrites only the `blocks` list: adds a block for a window at or over its line (`{provider, window, resets_at, source, observed_at, used_percent}`), drops a block whose provider now reads below its line or whose `resets_at` has passed, keeps blocks of providers it could not read, and keeps PM fields (`wake_scheduled`, `wake_note`, ...) on a block that persists; every other top-level key stays verbatim. Written atomically, only when the content changes. `collect()` stays pure.
5. **Lines per provider.** *Proposed:* one required key `usage.block_percent = {"claude": 90, "codex": 100, "antigravity": 100}` (decision 44, S32) instead of a Claude-only key, so all three lines are visible configuration.

### 3. Plan

Exists: `usage_snapshot.py` (Codex windows, `--claude-file` pass-through, Antigravity command assumed JSON), `codex_backend.validate_usage`, `session_context.py` blocks, `settings.example.json` rendered by `setup_project.py` (placeholders anywhere, `setup_project.py:452-460`). Missing: the status-line script, file-based Claude and Antigravity sources, unlimited handling, thresholds, the blocks writer, the SessionStart line.

1. N135: record the source changes above (Desktop status line unknown, `spend_limit`, units of `rate_limit_event` by experiment, `usage_report`, agy registration path, scheduled-task wording); source changes only.
2. `codex_backend.py`: extend `USAGE_KEYS` and `validate_usage` with the keys below; values in `codex_profiles.json`; refusal and non-default override tests next to the existing ones (`test_cli_worker_codex.py:565-618`).
3. `tools/agent-workflow/statusline.py`: read stdin JSON; if it has `rate_limits` write the Claude file, if it has `quota` write the Antigravity file (each with `written_at`, the raw object verbatim, and for Claude also a present-without-limits marker); print a short usage line; invalid config prints the refusal text as the line. Tests: both payloads, absent `rate_limits` (unlimited marker), empty or corrupt stdin, atomic replace, config refusal.
4. `usage_snapshot.py`: Claude and Antigravity sources as in questions 2-3, windows in the same entry shape as Codex (`label`, `window_minutes`, `used_percent`, `resets_at`, `state`, reset past `resets_at` = 0%); `unlimited: true` for a fresh source with no limit; `blocked` per window against `block_percent`; Antigravity command output: a JSON object or text is kept as `data` or `report`, no JSON assumed; the blocks writer in `main()`. Tests plus a property test (hypothesis 6.170.15 is installed) for the payload parsers.
5. `session_context.py`: one block right after the continuation note, `Quota blocks (<path>): <provider> <window> until <resets_at> (source)`; missing file: no block; malformed file: one line `quota blocks unreadable`; never raises. Tests.
6. `settings.example.json`: `"statusLine": {"type": "command", "command": "<VENV_PYTHON> -B -I <PROJECT_ROOT>/tools/agent-workflow/statusline.py"}` (forward slashes for Git Bash, per the docs); no allow rule in this phase.
7. Checks: ruff and `ruff format --check`, 3-10 mutants on a scratch copy, the full suite through `test_slot.py`, a live `usage_snapshot.py` on the dev machine (pasted).

**Finish checks (phase 1 part of the done check):** the full `tools/agent-workflow/tests` suite passes through `test_slot.py`; `usage_snapshot.py` prints `ok` or `unlimited` for Claude (worker events) and Codex, and for Antigravity `ok` when `antigravity_command` or its status-line file is present on the dev machine, otherwise `unknown` with the reason "no source configured" stated, not hidden; a test feeding a 90% Claude window writes a block and the SessionStart test prints it.

### Approval and probes (PM, 2026-11-15)

- PM approved the plan and answered questions 1-5 as proposed (record source per window; blocks list only, other keys byte-for-byte; `block_percent` refuses a missing provider or a value outside 0..100).
- Probe commands, each run once from the worktree, raw output under `.tmp/T-026/probes/`:
  - `agy -p /usage --output-format json`
  - `claude -p /usage --output-format stream-json --verbose`
- **Probe result (2026-11-15):** `agy -p /usage --output-format json` (agy 1.5.9) exited 0 but **started a model turn**: `.tmp/T-026/probes/agy-usage.json` holds `"status":"SUCCESS","response":"","num_turns":1,"usage":{"input_tokens":28103,"output_tokens":1095,"thinking_tokens":806,"cache_read_tokens":76856,...},"denied_actions":[{"action":"read_file",...}]`; stderr: "jetski: no output produced — a tool required the "read_file" permission that headless mode cannot prompt for, so it was auto-denied." So in this version `/usage` under `-p` is sent to the model, spends Antigravity quota and returns no report. Per the PM's rule the Claude probe was **not run** (`not_run`: the PM said stop if a probe starts a model turn; the Claude form could do the same). Item 8 therefore uses the status-line file and worker `rate_limit_event` only.
- **PM decision pending user review (decision 94, conservative and reversible):** `usage.antigravity_command` stays as it is (null in the tracked profile, so nothing runs by default); its docstring and N135 now say it starts a model turn in agy 1.5.9. The status-line file is the Antigravity source. Removing the command path is a product change left to the PM.

### Configuration impact

| Setting | Schema and source | Value in `codex_profiles.json` | Class |
|---|---|---|---|
| `usage.block_percent` | object with exactly `claude`, `codex`, `antigravity`, each a number above 0 and at most 100; `codex_backend.validate_usage` | `{"claude": 90, "codex": 100, "antigravity": 100}` | configurable (decision 44) |
| `usage.claude_statusline_file` | relative path without `..`, resolved under the project root (`_relative_dir`) | `.tmp/pm-state/claude-statusline.json` | configurable |
| `usage.antigravity_statusline_file` | same | `.tmp/pm-state/antigravity-statusline.json` | configurable |
| `usage.quota_blocks_file` | same; read by `usage_snapshot.py` and `session_context.py` (the hook loads `codex_backend` by file path and shows `not configured: usage.quota_blocks_file` on failure) | `.tmp/pm-state/quota-blocks.json` (the path ORCHESTRATION section 3 names) | configurable |
| Worker log folder for `rate_limit_event` | `cli_worker.JOBS` (`cli_worker.py:55`, a constant there); a test asserts the snapshot uses the same path | `.tmp/cli-jobs` | artifact-bound |
| Worker-log scan cap | existing `usage.session_file_limit` (newest-file count), reused | 8 | configurable (existing) |
| Freshness of status-line files without `resets_at` | existing `usage.stale_minutes` | 60 | configurable (existing) |
| Field names `rate_limits`, `five_hour`, `seven_day`, `spend_limit`, `used_percentage`, `resets_at`, `quota`, `remaining_fraction`, `remaining_amount`, `reset_time`, `reset_in_seconds`, `disabled`, `rate_limit_event`, `rateLimitType`, `resetsAt`, `utilization`; window lengths `five_hour` = 300 min, `seven_day` = 10080 min; `utilization` × 100 | the CLIs' own output (sources above) | n/a | protocol-bound |
| `statusLine` entry | `settings.example.json`, rendered by `setup_project.py apply` | command above | configurable (template) |
| Allow rules | none in phase 1 (item 6 is phase 2) | n/a | n/a |

## Steps (phase 1)

1. Probes: `agy -p /usage --output-format json` ran a model turn; Claude probe not run (see "Probe result").
   Consequence: Antigravity reads the status-line file; the command path stays, documented as spending quota.
2. `codex_backend.py`: four required `usage` keys validated (`validate_block_percent`, `_relative_dir`); values in `codex_profiles.json`.
   Refusal and override tests added in `test_cli_worker_codex.py`.
3. `statusline.py --source claude|antigravity`: stores `rate_limits` / `quota` verbatim with `written_at`, atomic write, always exit 0.
   No write when the member is absent or null, so an early empty payload never hides a reading.
4. `usage_snapshot.py`: common window shape with `source`, newest reading per window for Claude, Antigravity buckets, `unlimited`, `blocked`.
   `main()` alone writes the `blocks` list of the quota file by splicing that one member; `collect()` stays pure.
5. `session_context.py`: a quota block right after the continuation note; missing file no block; bad file or config one line.
6. `settings.example.json` statusLine entry; N135 "Re-read 2026-11-15"; README lines for the snapshot, the new file and local profiles.

## Completion report (phase 1)

- **Work completed:** items 1, 2, 3, 4 and 8 as planned, with the PM's answers applied. Item 8: the Claude reset time inside Desktop comes from the workers' `rate_limit_event` in `.tmp/cli-jobs/*/NNN.jsonl` (weekly window only so far) and, in the terminal CLI, from the status line (both windows).
- **Result and evidence:**
  - Tests: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_quota_tooling.py tools/agent-workflow/tests/test_cli_worker_codex.py tools/agent-workflow/tests/test_session_context.py -x` → `384 passed, 2 skipped in 112.52s`.
  - Full suite (done check): first run stopped by the 10-minute background limit at 70% (no failure so far); rerun `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests` → `2 failed, 4803 passed, 10 skipped in 725.88s`. Both failures (`test_the_real_checkout_has_no_failures`, `test_apply_binds_a_copy_and_is_idempotent`) were "foreign: 2 previous-project token(s)": absolute `D:/...` paths I had written in this task file. Fixed (placeholders and relative paths); the two tests rerun: see the last line of this report.
  - Mutants (scratch copy `.tmp/T-026/mut`, removed after; log `.tmp/T-026/mutants.txt`): 9 of 9 killed — blocked `>=`→`>`, `fraction * 100`→`fraction`, reset-passed keep/drop inverted, below-line drop removed, newest-reading comparison inverted, splice off by one, statusline null check → `not in`, `block_percent` lower bound `0 <`→`0 <=`, SessionStart "reset passed" comparison inverted. No survivors.
  - Live snapshot on the dev machine (worktree code in a scratch root holding copies of the 8 newest worker logs with their mtimes, tracked profile; raw output `.tmp/T-026/live-snapshot.json`, collected 2026-11-15T06:24:01Z):
    - `claude`: `ok`, window `weekly` 15.0% used, `resets_at` 2026-11-18T14:00:00+00:00, `source` "worker rate_limit_event ...\t026-opus-dev\001.jsonl", `blocked: false` (line 90). No 5-hour window: no status-line file there, and worker events carry only `seven_day`.
    - `codex`: `ok`, `weekly` 10.0% used, `resets_at` 2026-11-21T13:24:27+00:00, plan `prolite`, `blocked: false` (line 100).
    - `antigravity`: `unknown`, "no status-line file ...antigravity-statusline.json and no usage.antigravity_command configured".
    - `quota_blocks`: `unchanged`, 0 blocks (nothing blocked, so no file created).
  - **Done check, snapshot part: not met for Antigravity.** Its `unknown` is caused by the missing status-line file, which appears only after the user or PM registers the snippet below and runs `agy` interactively. Claude and Codex meet it.
- **Important changes and reasons:**
  - `usage.block_percent` replaces a Claude-only key (PM answer 5); the three path keys are required (PM review: no silent defaults).
  - `antigravity_snapshot` keeps a non-JSON reply as `report` text instead of `unknown` (item 3: no JSON assumed); two existing assertions changed accordingly in `test_the_antigravity_usage_command_failures_are_unknown_never_zero`.
  - Existing `us.main()` tests now patch `us.ROOT`/`us.JOBS` so the command's quota-file write lands in the test folder.
  - Outside the brief's file list but required by it: `codex_backend.py` and `codex_profiles.json` (the approved config keys) and `tools/agent-workflow/README.md` (the snapshot's own documentation would otherwise be wrong).
- **Problems, risks and remaining work:**
  - **Live cost risk (urgent for the PM):** the main checkout's `.tmp/pm-state/codex.local.json` sets `usage.antigravity_command` to the installed `agy.EXE`, so each `usage_snapshot.py --config` with that file runs an Antigravity model turn (probe above). Set it to `null` when adding the four new keys there (the local file is refused until it has them).
  - Claude 5-hour window: only the terminal status line supplies it; inside Desktop it stays absent until `claude -p /usage` is checked (not run).
  - A turn log has no event time, so a worker reading is dated by the log's mtime (later than the event by up to the turn's length).
  - `quota-blocks.json`: blocks the PM wrote by hand under another window name (for example `weekly, all models`) are kept until their reset passes, beside the snapshot's own `weekly` block.
  - After integration the PM runs `setup_project.py apply` to render the `statusLine` entry into `.claude/settings.local.json` (I did not touch the live file).

### Antigravity status-line registration (user or PM step, PM answer 2)

Target: `~/.gemini/antigravity-cli/settings.json` in the user's home folder. Add at top level, with `<VENV_PYTHON>` and `<PROJECT_ROOT>` replaced by the absolute paths of the main checkout, written with backslashes on Windows, where agy runs the command with `cmd /c` (not yet run by anyone):

```json
"statusLine": {
  "type": "command",
  "command": "<VENV_PYTHON> -B -I <PROJECT_ROOT>\\tools\\agent-workflow\\statusline.py --source antigravity"
}
```

### Research evidence

The Step 0 table above, plus: agy 1.5.9 probe (`.tmp/T-026/probes/agy-usage.json`, experiment: model turn); worker `utilization` 0.08→0.15 across 2026-11-15 logs (experiment: fraction 0..1).

### Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Blocked line per provider | `usage.block_percent` | `codex_backend.validate_block_percent`: exactly claude/codex/antigravity, number, 0 < x ≤ 100 | `codex_profiles.json` 90/100/100 | override `test_the_quota_settings_are_honoured` (75.5/99/100), `test_collect_marks_windows_at_or_over_each_providers_line` (95.5); refusals in `REFUSALS` (missing, unknown, not object, 0, 100.5, -1, bool, string) | configurable |
| Claude status-line file | `usage.claude_statusline_file` | `_relative_dir` + `resolve_in_root` after link resolution | `.tmp/pm-state/claude-statusline.json` | override `test_the_quota_settings_are_honoured`; refusals `../outside.json`, missing; `test_a_configured_path_that_resolves_outside_the_root_is_refused` (skipped here: no symlink right) | configurable |
| Antigravity status-line file | `usage.antigravity_statusline_file` | same | `.tmp/pm-state/antigravity-statusline.json` | override (backslash path); refusals `C:/abs.json`, missing | configurable |
| Quota blocks file | `usage.quota_blocks_file` | same; hook reads it through `load_usage_settings` | `.tmp/pm-state/quota-blocks.json` | override `state/blocks.json`; refusals `/abs/quota.json`, `<path>`, missing; hook `not configured` line tested | configurable |
| Worker log folder | `usage_snapshot.JOBS` = `cli_worker.JOBS` | equality test | `.tmp/cli-jobs` | `test_the_snapshot_reads_worker_events_from_the_launchers_job_folder` | artifact-bound |
| Worker-log scan bound | `usage.session_file_limit` (existing) | `_positive_int` | 8 | `test_claude_worker_events_rejected_reset_and_bounded` (limit 1, limit 0 refused) | configurable |
| Freshness without reset time | `usage.stale_minutes` (existing) | `_positive_int` | 60 | `test_claude_without_any_source_...` (60 vs 180) | configurable |
| Status-line field names, window lengths 300/10080, `utilization × 100`, `rate_limit_event` marker | none | n/a | n/a | parser tests and property test `test_payload_parsers_never_raise` | protocol-bound |
| Replace retries 5 × 0.05 s, RFC 8259 white space, status line 160 ASCII chars | none | n/a | n/a | atomic write and ASCII line tested | protocol-bound / layout |
| `statusLine` entry | `settings.example.json` | rendered by `setup_project.py` placeholders | template | full suite (setup tests) | configurable (template) |

### not_run

- `claude -p /usage --output-format stream-json --verbose`: the PM said stop if a probe starts a model turn; the agy one did.
- `ruff check` / `ruff format --check`: ruff is not installed in `.venv` and installs are not allowed; `py_compile` of the four changed modules passed.
- The symlink refusal test skipped (Windows user without symlink right); the refusal logic also runs under the `_relative_dir` tests.

### Noticed, not done

- `docs/note/N135` line on Antigravity still quotes the 2026-11-13 claims above the new section; the new section corrects them (source changes only, per scope).

### Final check

After the task-file fix: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests -k "test_the_real_checkout_has_no_failures or test_apply_binds_a_copy_and_is_idempotent"` → `2 passed, 4813 deselected in 7.88s`. With the full run's other 4803 passes, the suite is green on this tree; the full suite was not rerun end to end after this one task-file edit (`not_run`: 12 minutes; only this Markdown file changed).

## Fix round 1 (review findings; Claude Opus 5.5, fresh worker, 2026-11-15)

Inputs: Codex `high` FAIL (`.tmp/T-026/review-codex.md`, F1-F9) and Gemini Flash PASS with 2 P2 and 1 P3 (`.tmp/T-026/review-gemini-p1.md`). R-IDs: R-021, R-015, R-025.

### Step 0 (small fix round, put atop and continued: the PM brief fixes the plan)

- Unknowns: how the PM writes `quota-blocks.json` (Edit tool, no OS lock), so a lock alone cannot exclude a PM write; Windows `fromtimestamp` range (found: it stops at year 3001, so a "datetime can hold it" check differed between OSes).
- Plan: one validator per source plus one provider rule; one contained-path helper reused by the three consumers; one locked byte-splice writer that re-reads before each replace. Finish: every finding fails before and passes after, mutants, full suite.

### Root cause and design

Phase 1 read missing or malformed evidence as a healthy reading, and treated the quota files as private to one writer. Now:

- **Readings** (`usage_snapshot.py`): `bounded` (finite JSON number in a range, never a bool), `epoch` (seconds up to the end of year 9999 by epoch arithmetic, the same on every OS; a millisecond stamp is refused), `field` (absent vs present-but-invalid). Validators: `claude_status_windows` (`used_percentage` 0..100), `rate_limit_windows` (`utilization` 0..1, documented `status` values; the top-level fields fill the `rateLimitType` window's missing ones and a top-level `rejected` holds, F6), `antigravity_windows` (`remaining_fraction` 0..1, `remaining_amount` ≥ 0, ISO `reset_time`, bool `disabled`), `windows_of` (Codex, same ranges). A malformed window is `state: unknown` with a `reason`, never skipped.
- **Provider rule** (`provider_result`): `ok` only when a window holds a valid reading; `unlimited` only from a fresh, schema-valid no-window reading (status-line member an object, `written_at` a time not after now; Codex `rate_limits` an object or explicit null in a fresh row); otherwise `unknown` (F1, F9, Gemini P3). A worker event without a window name is no evidence.
- **Block transitions** (`updated_blocks`, `clears`): a blocked window adds or refreshes; otherwise a block clears only when its own reset passed, its window reads `reset`, or a fresh `ok` reading below the line no older than the block's `observed_at`. Unknown, stale and disabled windows keep it; unknown providers keep all their blocks (F2).
- **Paths**: `codex_backend._state_file` stores each quota path with `/` and requires it under `.tmp/pm-state/` (a `:` refused on every OS; F5, F7). `usage_snapshot.state_path` splits either separator again, requires the state folder, refuses a link or junction at every component from the root down to the file (reusing `purge_tmp._is_link`), creates folders only on request and checks each one it creates. `statusline.py`, `main` and the SessionStart hook all use it (F8).
- **Writer** (`write_quota_blocks`, `locked_write`, `planned_blocks`): the whole read, merge and replace runs under `<file>.lock` (the OS lock of `hooks/git_ledger.py`, `_lock`/`_unlock`, held across every retry). Before each replace the file is read again; a change by another writer, or a sharing violation, restarts the merge from the new bytes (F3). Bytes are decoded without newline translation and the new value uses the file's own line ending (F4); a non-UTF-8 file is left untouched.

### Before and after (`.tmp/T-026/fix1/`)

- Before: the phase 1 modules (`git show :<path>` of `usage_snapshot.py`, `statusline.py`, `codex_backend.py`, `hooks/session_context.py`) in a scratch copy with the new tests: `before.txt` → `59 failed, 29 passed in 1.91s`; all 16 `test_fix1_*` functions fail, each on its finding (for example F1 `assert 'unlimited' == 'unknown'`, F3 `assert ('old' == 'NEW-PM-UPDATE'`, F7 `PurePosixPath('/srv/project/.tmp\\pm-state\\a.json')`, F8 `'- outside_fixture None blocked until None'` printed by the hook).
- After: `after.txt` → `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_quota_tooling.py tools/agent-workflow/tests/test_session_context.py` → `109 passed in 4.71s` (before the two mutant-driven test additions below; they pass in the full run).
- Finding → test: F1 `test_fix1_{claude,antigravity}_malformed_or_stale_status_line_is_unknown`, `test_fix1_codex_unlimited_needs_a_fresh_valid_row`, `test_fix1_a_worker_event_without_a_window_is_no_evidence`; F2 `test_fix1_a_stale_reading_without_reset_keeps_an_active_block`, `test_block_transitions` (11 rows); F3 `test_fix1_a_pm_edit_during_the_merge_survives`, `test_fix1_a_pm_edit_during_a_sharing_violation_retry_survives`, `test_a_held_lock_stops_the_write_and_leaves_the_file`, `test_two_writer_processes_both_land` (two real processes, `CREATE_NO_WINDOW`); F4 `test_fix1_crlf_outside_blocks_is_kept`; F5 `test_fix1_a_status_line_path_outside_the_state_folder_is_refused` and five new `REFUSALS` rows; F6 `test_fix1_a_partial_unified_window_keeps_the_top_level_rejection_and_reset`; F7 `test_fix1_a_backslash_path_means_the_same_file_on_posix`; F8 `test_fix1_a_link_on_a_quota_path_is_refused_everywhere[.tmp, .tmp/pm-state]` (real junctions; hook, statusline, snapshot); F9 and Gemini P3 the four `test_fix1_*_invalid_*_are_unknown` tables, `test_valid_boundary_values_are_accepted`, and the property test `test_payload_parsers_never_raise_and_only_pass_in_range_values` (NaN and infinity included).
- Earlier tests changed because the rule changed: the no-name worker event is now `unknown` (was unlimited); the `written_at` message; the `window()` helper gains `state` and `fresh`; `resolve_in_root` test replaced by a `state_path` test; the created-only test makes its folder (callers make it through `state_path(create=True)`); `test_the_quota_settings_are_honoured` uses paths under `.tmp/pm-state` and checks the `/` form.

### Mutants (`.tmp/T-026/fix1/mutants.py`, scratch copy, removed after; `mutants.txt`, `mutants-rerun.txt`)

18 mutants: `usage_snapshot.py` 10, `codex_backend.py` 3, `statusline.py` 2, `hooks/session_context.py` 3. First run 16 killed; survivors 6 (top-level `rejected` override removed) and 18 (hook reads without BOM) showed two missing cases, now tested (window entry saying `allowed` under a top-level rejection; the PM's BOM file in the hook). Rerun: both killed. 18 of 18 killed.

### Live read-only check (`.tmp/T-026/fix1/live.py`, output `live.json`, 2026-11-15T07:21Z)

Real data under the new validation: Codex `ok`, weekly 11.0%, fresh; Claude `ok` from the main checkout's worker events, weekly 17.0% and a new window name `seven_day_overage_included` 0.0%, both resetting 2026-11-18T14:00Z. Nothing written.

### Configuration audit (rows added or changed)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Quota file paths (three keys) | `usage.claude_statusline_file`, `usage.antigravity_statusline_file`, `usage.quota_blocks_file` | `codex_backend._state_file`: relative, no `..`, no `:`, under `.tmp/pm-state/`, stored with `/` | unchanged values | override `test_the_quota_settings_are_honoured` (sub-folder, backslashes, `./` and `//`); refusals `notes/quota.json`, `.tmp/pm-state`, `.tmp/other/quota.json`, `a.json:stream`, `C:a.json` | configurable |
| State folder `.tmp/pm-state` | `codex_backend.STATE_DIR` | used by `_state_file` and `state_path` | n/a | refusal rows above | artifact-bound (ORCHESTRATION section 3) |
| Lock file `<quota file>.lock` | `usage_snapshot.LOCK_SUFFIX` | n/a | n/a | `test_a_held_lock_stops_the_write_and_leaves_the_file` | artifact-bound |
| Lock wait 5 s | `usage_snapshot.LOCK_WAIT_SECONDS` | n/a | n/a | same test (0.2 s override) | safety-bound: stops a hung holder; the section is milliseconds |
| Percent 0..100, fraction 0..1, amount ≥ 0, epoch up to 9999-12-31, `status` values, top-level event fields | `bounded`, `epoch`, `EVENT_STATUSES`, `EVENT_FIELDS`, `UNIX_EPOCH` | n/a | n/a | invalid tables, boundary test, property test | protocol-bound (the CLIs' documented units; T-026 experiment for the event) |

### Problems, risks and remaining work

- A writer that does not take the lock (the PM editing the file by hand) can still land in the instant between the last re-read and the replace; the window is now that instant, not the whole merge. Closing it needs PM writes to take `<file>.lock` (a follow-up, not in scope).
- The link check is done before the open: a link created between the check and the open is not caught (needs an attacker on the local disk; same class as `purge_tmp`).
- `write_quota_blocks` reuses the private `_lock`/`_unlock` of `hooks/git_ledger.py` as the brief asked; a rename there breaks this writer (the lock tests would fail).
- Worker events now carry a third window name, `seven_day_overage_included` (live check); it is shown and blocked like any window.

### not_run

- `ruff check` / `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed. Modules parse (`ast.parse`) and import in the tests.
- Native Linux: the POSIX join is simulated with `PurePosixPath`; the symlink branch of `make_link` runs only on POSIX.
- `agy` and `claude` commands: not run (brief).

### Final check (fix round 1)

- Full suite, after the mutant-driven test additions: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests` (environment of footer rule 1, own `--basetemp`) → `4875 passed, 10 skipped in 896.36s (0:14:56)` (`.tmp/T-026/fix1/full-suite.txt`). Skips are host conditions: symlink right (2), POSIX modes and names (2), 8.3 names, FIFO, Git path refusals (2), the temp folder inside a repository, an unbound runtime module.
- This section was written while that run was in progress; the two tests that read task files were rerun afterwards: see the next line.
- `... pytest ... tools/agent-workflow/tests -k "test_the_real_checkout_has_no_failures or test_apply_binds_a_copy_and_is_idempotent"` → `2 passed, 4883 deselected in 7.64s`.
- Child processes: the new two-writer test and `mutants.py` start children with `CREATE_NO_WINDOW`; no product code starts a process in this round.
- Changed files this round: `usage_snapshot.py`, `statusline.py`, `codex_backend.py`, `hooks/session_context.py`, `README.md`, `tests/test_quota_tooling.py`, `tests/test_cli_worker_codex.py` (all under `tools/agent-workflow/`), this task file.

## Fix round 2 (Codex retest RT1-RT4; Claude Opus 5.5, fresh worker, 2026-11-15)

Input: `.tmp/T-026/retest1/review.md` (FAIL, RT1-RT4), probes `.tmp/T-026/retest1/test_fresh_probes.py`. R-IDs: R-021, R-015, R-025.

### Step 0 (small fix round, put atop and continued: the PM brief fixes the plan and the approach)

- Unknowns: which Codex `rate_limits` members are window slots (answered by a live row, below); whether a clock-skew tolerance is needed for "not after now" (no: every observation time is a local file time or a local write, and `now` is taken once before the scan, so a skew could only make a reading refused, never accepted); how the retest's RT4 probe, an unlocked write landing at the commit, can be kept (answered by the claim/publish commit, below).
- Plan: (1) the `Observation` type and `observe` gate, all four sources through it; (2) cycle-ordered `clears` and ordered refresh; (3) one `transact` for every writer, a claim/publish commit, `pm-set`, the hook reading through the commit; (4) docs naming `pm-set`. Finish: probes green, class tests, mutants, full suite.

### Root cause and design

Round 1 validated one field at a time and compared a block only with "a window", never with a whole, dated observation. Now:

- **Observation** (`usage_snapshot.Observation`, `observe`): every reading of every source (Codex row, Claude status line, worker `rate_limit_event`, Antigravity status line) becomes one `Observation(source, at, fresh, windows, no_limit)` or a refusal reason. `observe` refuses a time that is missing, not representable or later than `now` (RT2), computes freshness once, tags each window with `source` and `observed_at`, and lets a reading state "no limit" only when it is fresh and has no window. `provider_from` picks the newest observation per window and lists refusals under `refused`; a future Codex row or worker log is refused, so it can neither win "newest" nor clear a block.
- **Codex slots** (RT1): `primary` and `secondary` are window slots (protocol-bound, live row of 2026-11-15: `"primary":{"used_percent":26.0,"window_minutes":10080,"resets_at":1792049283},"secondary":null,"credits":{...}`). A slot that is not null is type-checked before any "no limit" inference: a list, bool, string, number or `{}` is an `unknown` window under the slot name; only null or absent slots state no limit.
- **Block transitions** (RT3, `clears`, `updated_blocks`): a reading clears a block only when it was observed strictly after the block's `observed_at` and its reset time is later than the block's observation (the same or a later cycle); then it clears when the window has reset since, or when it is fresh, `ok` and below the line. A block's own `resets_at` passing still clears it. A blocked reading refreshes a block only when it is not older than the block. A block without a valid `observed_at` (an old hand-written one) is never cleared by a reading, only by its reset or by `pm-set --remove-block`.
- **One transaction** (RT4, `transact`, `locked_write`): both writers, the snapshot and the new PM command `usage_snapshot.py pm-set`, run under `<file>.lock` (reused `git_ledger._lock`). The commit claims the file by renaming it to `<file>.claimed`, checks that the claimed bytes are the ones the plan read (a hand edit that landed after the read is put back and planned again), and publishes with `os.link` (create-only, so a file created after the claim is never overwritten). The next writer recovers a claim left by a killed commit; `read_current` lets the lock-free SessionStart hook read the claim name in that moment. A hand edit without the lock is documented as out of contract (PM_POLICY section 1, ORCHESTRATION section 3, README).
- **`pm-set`**: `--text NAME TEXT`, `--json NAME VALUE`, `--unset NAME`, `--block PROVIDER WINDOW`, `--remove-block`. Refused before reading (exit 2): no edit, a name twice, `blocks` as a member, `provider`/`window` as fields, a provider outside claude/codex/antigravity, a `resets_at` or `observed_at` that is not an ISO time, a future `observed_at`, removing `observed_at`. A block it creates gets `observed_at` now and `written_by: pm`. Output: one JSON report; exit 1 when nothing could be written (for example the lock stayed busy).

### Before and after (`.tmp/T-026/fix2/`)

- Retest probes (a copy of the retest's file in `.tmp/T-026/fix2/`, so its scratch copy of the modules lands there): before `probes-before.txt` → `13 failed, 100 passed in 2.76s` (the 13 of the retest); after `probes-after.txt` → `113 passed in 3.23s`.
- Own class tests (`tests/test_quota_tooling.py`, `test_fix2_*`): malformed-slot table per source (4 sources × 6 values), Codex null slots and a bad slot beside a good one, a future observation per source (refused, block kept), a future reading beside a real one (the real one wins, the refusal listed), the cycle-ordering table (10 rows × 4 sources), `pm-set` edits (BOM and CRLF kept) and 11 refusals, `pm-set` inside the snapshot's transaction (refused with `stayed busy`, lands afterwards), `pm-set` and the snapshot in two real processes (`CREATE_NO_WINDOW`), a hand edit before the claim, a file created after the claim, a killed commit in both states, a file system without hard links (the claim is put back), the hook during a claim.
- `tools/agent-workflow/tests/test_quota_tooling.py` + `test_session_context.py`: `own7.txt` → `201 passed in 18.28s`.
- Earlier tests changed because the rule changed: `test_block_transitions` rows now give the block an `observed_at` (a block without one is kept: new row), the same-instant row is `kept`, a `reset` row gives the window a reset after the block; `test_quota_blocks_owns_only_the_blocks_list` clears with a later reading; `test_main_writes_the_blocks_file_and_reports_it` dates its worker log before the real now (its old `NOW` lies in 2027, now refused as future); the sharing-violation double writes to the file being claimed (the commit now renames the file instead of replacing it).

### Mutants (`.tmp/T-026/fix2/mutants.py`, scratch copy, removed after; `mutants.txt`)

13 of 13 killed: `usage_snapshot.py` 11 (future refusal off, slot check off, strict ordering `<=`→`<`, cycle check off, refresh ordering off, claim check off, create-only publish → replace, claim recovery off, no-limit from any observation, provider check off, `read_current` without the claim name), `hooks/session_context.py` 2 (plain read instead of `read_current`, missing file prints text). `codex_backend.py` and `statusline.py` are unchanged this round.

### Configuration audit (rows added this round)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Codex window slots `primary`, `secondary` | `usage_snapshot.CODEX_WINDOW_SLOTS` | n/a | n/a | `test_fix2_a_malformed_window_slot_is_unknown_never_unlimited`, `test_fix2_codex_null_slots_...` | protocol-bound (live Codex row) |
| Observation not after now, no tolerance | none: no tolerance key is needed (local clocks only; a skew can only refuse) | `observe` | n/a | `test_fix2_an_observation_dated_after_now_is_refused` (4 sources) | safety-bound |
| Claim name `<file>.claimed` | `usage_snapshot.CLAIM_SUFFIX` | n/a | n/a | `test_fix2_a_killed_commit_is_recovered_and_readers_see_the_claim` | artifact-bound |
| `pm-set` providers, block identity and time fields | `PROVIDERS`, `BLOCK_IDENTITY`, `BLOCK_TIMES` | `pm_edit_problem` | n/a | `test_fix2_pm_set_refusals_change_nothing` (11 rows) | artifact-bound (the file's schema) |
| `observed_at` of a block `pm-set` creates | the call's time unless `--text observed_at` is given | ISO, not after now | n/a | `test_fix2_pm_set_edits_top_level_and_block_fields`, refusal rows | design (ordering needs a time) |

### Problems, risks and remaining work

- A hand edit without the lock that lands inside the commit itself (between the claim check and the publish, two system calls) can still be lost; a hand edit is now out of contract and documented so. A hand edit made by an editor that holds the file open is refused by Windows at the claim (retried), on POSIX it can write into the claimed file.
- Between claim and publish the file is absent under its name for two system calls; the hook reads the claim name then. Another lock-free reader (the PM's Read tool) can see it missing in that instant.
- `os.link` needs a file system with hard links (NTFS, ext4); on one without, the snapshot reports `error` and puts the claimed file back.
- Blocks written by hand before this round have no `observed_at`, so a reading never clears them; they go when their reset passes or by `pm-set --remove-block` (PM_POLICY step 3 now says so).

### not_run

- `ruff check` / `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed.
- Native Linux (the POSIX `flock` and `os.link` paths run only on Windows here).
- `agy` and `claude` commands, the live `usage_snapshot.py` (it would write `.tmp/pm-state/` of this worktree): not run.

### Final check (fix round 2)

- Full suite: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests` (footer rule 1 environment, own `--basetemp`) → `4966 passed, 10 skipped in 991.28s (0:16:31)` (`.tmp/T-026/fix2/full-suite.txt`; skips are the host conditions listed in round 1). That run started before two last edits (`put_back` restores the claim without hard links, its test, and this task file), so I ran the affected tests again on the final tree: `... pytest ... tools/agent-workflow/tests -k "test_the_real_checkout_has_no_failures or test_apply_binds_a_copy_and_is_idempotent or quota_tooling or session_context"` → `205 passed, 4772 deselected in 12.30s` (`final-rerun.txt`).
- Child processes: the new two-process test and `mutants.py` start children with `CREATE_NO_WINDOW`; product code starts no new process.
- Changed files this round: `tools/agent-workflow/usage_snapshot.py`, `tools/agent-workflow/hooks/session_context.py`, `tools/agent-workflow/README.md`, `tools/agent-workflow/tests/test_quota_tooling.py`, `.agents/policy/PM_POLICY.md` and `.agents/ORCHESTRATION.md` (the docs that told the PM to edit the file; brief item c), this task file.

## Fix round 3 audit (Step 0; Claude Fable 5.2 `high`, fresh worker, 2026-11-15; waiting for approval, no code changed)

Input: `.tmp/T-026/retest2/review.md` (FAIL, RT5-RT9). R-IDs: R-021, R-015, R-025. Evidence gathered: `.tmp/T-026/fix3/probes-before.txt` (the retest file unchanged on the round 2 code: `7 failed, 89 passed in 3.70s`, the retest's seven) and `.tmp/T-026/fix3/experiment.txt` (script `experiment.py`, real NTFS, Python 3.12.10). Line numbers are those of the round 2 `usage_snapshot.py`.

### Root causes

| Finding | Root cause (source-verified) |
|---|---|
| RT5 | Round 2 gave up the file's stable name: a commit renames it to `.claimed` (`:1065`) and publishes with `os.link` (`:1069`), to detect a writer without the lock. Retest 1 had already said that case cannot be protected ("PM edits must use the same locked transaction; rereading cannot protect an unlocked writer"). Once the name can be absent while the file exists, the lock-free `read_current` (`:990`) has no proof of absence; three tries are not one. |
| RT6, RT7 | There is no state machine, only three places that each know part of it: `updated_blocks` (`:871`), `clears` (`:913`), `planned_pm_edit` (`:1140`). `planned_pm_edit` knows "create" and "set fields", not "re-assert" (`:1165`). `updated_blocks` applies "an unknown provider never clears" (`:890`) before the block's own reset, which is not a reading. |
| RT8 | `parse_time` (`:107`) calls `.timestamp()` on whatever `fromisoformat` returns; a text without an offset is a naive datetime, read in the host zone (experiment: `2026-11-20T14:03:00` lands 25200 s away from UTC on this UTC+7 host; a date-only text is naive too). Every ISO time of the tool passes this parser or the hook's own copy (`hooks/session_context.py:378`). |
| RT9 | Errors are handled per call site: `transact` catches `TimeoutError` only (`:1015`), `locked_write` four named subclasses, `main` has a catch (`:1297`), `pm_set` none (`:1219`); `lock.open` (`:1012`) is outside all of them. |

### (a) Every path that reads `quota-blocks.json`

| # | Reader | Code | Lock | "No block" without proof? |
|---|---|---|---|---|
| R1 | SessionStart hook | `session_context.quota_block:357` → `read_current:990` | none | **Yes (RT5).** Path, claim, path, each unlocked; all three can miss while a block exists throughout → `None` → empty text (`:358-359`). |
| R2 | Snapshot CLI | `main:1296` → `transact` → `locked_write:1055` | `<file>.lock` | No, among lock-taking writers: after `recover_claim` a missing file under the lock is no file. |
| R3 | `pm-set` CLI | `pm_set:1219` → same | same | No. |
| R4 | Status line `statusline.py` | does not open this file: `record:77-91` writes only its own source file | n/a | n/a (the retest agrees, review line 20). |
| R5 | `cli_worker.py:2035`, `:2117` | `collect` and the ledger helpers only | n/a | n/a. |
| R6 | Out of code: the PM's Read tool, an editor | none | Yes in round 2: the name is absent between claim and publish (disclosed there). |

Related, same question: the two status-line source files are read by `status_observation:547`. A missing or unreadable file is a refusal → provider `unknown`, which never clears a block and is never `unlimited`: already fail-closed, on a stable name replaced with `os.replace`.

**Proposed read contract (one, for every reader):**

1. **Stable name.** A commit is a temp file plus `os.replace(temp, path)`; nothing renames or unlinks `path`. `CLAIM_SUFFIX`, `claim_path`, `read_current`, `recover_claim`, `put_back` and the `os.link` publish are removed. The round 1 re-read before the replace stays (F3: it narrows, never closes, the window of a writer without the lock; that writer is out of contract, PM_POLICY section 1).
2. **One lock holder function.** `locked(path, action)` takes `<file>.lock` through `git_ledger._lock` with the existing `LOCK_WAIT_SECONDS` bound. It is the only way code opens the file: `transact` (read, plan, replace) and a new `read_quota_file(path)` for the hook.
3. **Three results, no fourth.** Bytes; *proven absent* (the state folder does not exist, or the file is missing while the lock is held); *unreadable* (lock busy past the bound, the lock path is a link, any other `OSError`, bad UTF-8, bad JSON, wrong shape). The hook prints `Quota blocks file ... unreadable: <reason>` for the third and empty text only for a proven absence or a proven empty `blocks` list.

Why it holds on both systems: the proof is the lock, not rename semantics: no commit overlaps a locked read, and the name never goes away. For readers outside the code the stable name is what remains: experiment, one process replacing in a loop and one reading lock-free for 3 s: 3438 reads, 0 missing, 0 torn, 647 `PermissionError` (never "absent"); `os.replace` over a file held open raises `PermissionError`, which the writer already retries. Linux: `rename(2)` replaces the name atomically (assumption from the man page, not re-read: no network; the proof does not rest on it). Not run: native Linux. Cost: behind a hung writer the hook waits 5 s and prints `unreadable` (hook timeout 30 s, `settings.example.json:90`); the hook creates the empty lock file when the state folder exists. The round 2 scheme never ran outside this worktree (the patch is uncommitted: `git diff --cached --stat`), so no `.claimed` file needs migrating.

### (b) Every time value that enters the tool

| # | Value | Enters at | Form | Today | Rule after the fix |
|---|---|---|---|---|---|
| T1 | Codex row `timestamp` | `last_token_row:408` | ISO text | naive read in host zone; present but invalid falls back to the file mtime as "no timestamp" | aware only; absent or null → mtime; present but refused → the observation is refused |
| T2, T5, T6 | Codex `resets_at`, Claude `resets_at`, worker `resetsAt` | `:230`, `:247`, `:294` | epoch seconds | `epoch` | UTC by definition; unchanged |
| T3 | session file and worker log mtime | `:421`, `:597` | epoch seconds | checked by `observe` | unchanged |
| T4, T7 | status-line `written_at` (Claude, Antigravity) | `status_observation:552` | ISO text | naive accepted | aware only, else "lacks a valid written_at" |
| T8 | Antigravity `reset_time` | `:314` | ISO text | naive accepted | aware only, else window `unknown`, "invalid reset_time" |
| T9 | block `resets_at`, `observed_at` in the file | `:897`, `:901`, `:922`, `:925` | ISO text | naive accepted | aware only; a refused value counts as absent for the transitions and the hook marks it |
| T10 | the same, in the hook | `hooks/session_context.py:378` | ISO text | own copy of the parser; naive accepted; invalid shown without a mark | the hook calls the one parser; refused → "reset time is not a time with a UTC offset" |
| T11 | `pm-set --text/--json resets_at`, `observed_at` | `pm_edit_problem:1132` | ISO text | naive accepted (RT8), stored as typed | aware only (exit 2); stored normalized to UTC |
| T12 | `--claude-file` mtime | `claude_snapshot:693-699` | epoch seconds | a future mtime is clamped to age 0, so it reads fresh | later than now → `unknown` (the `observe` rule) |
| T13 | `now` | `time.time()` at `:1211`, `:1280`, hook `:492`; `collect(now=)` | epoch seconds | fine | unchanged |
| T14 | `written_at` written by `statusline.py:89,105` | `datetime.now(UTC)` | aware | unchanged |
| — | Antigravity `reset_in_seconds`; flags `--config`, `--codex-home`, `--claude-file`, `--source`, `--block`, `--unset`, `--remove-block` | not read, or no time | | | |

One parser: `parse_time(text)` returns UTC epoch seconds only for a string that `fromisoformat` reads **with** an offset (`utcoffset()` not None) and that `iso` can hold; `iso_time` folds into it; the hook imports it. Every time the tool writes goes through `iso` (`+00:00`).

### (c) The block state machine: one function, `block_step(block, now, reading=None, pm=None)`

Terms: *Ob*, *Rb* = the block's valid `observed_at`, `resets_at` (aware; else absent); *legacy* = no valid Ob; *reading* = the snapshot's window for the block's provider and window, or none (provider `unknown` or unread, window not reported), with *Ow*, *Rw*; *low* = state `ok`, fresh, not blocked. First matching row wins. `updated_blocks` and `planned_pm_edit` become callers.

| # | Block | Event | Result | Findings |
|---|---|---|---|---|
| 1 | none | blocked reading | record from the reading (`observed_at` = Ow) | phase 1 item 4 |
| 2 | none | `pm-set --block` | record, `observed_at` = given or now, `written_by: pm` | RT4 |
| 3 | any | blocked reading, Ow ≥ Ob or legacy | re-assert: the reading's fields replace, PM fields stay | F2 |
| 4 | any | `pm-set --block` that assigns `resets_at` | re-assert: fields set, `observed_at` = given or **now** | **RT6** |
| 5 | any | `pm-set --block`, other fields only | metadata edit: `observed_at` untouched | **RT6** |
| 6 | any | `pm-set --block --remove-block` | removed | round 2 |
| 7 | Rb < now | any snapshot run: provider read or unread, legacy or current, any reading except row 3 | expired: removed (a still-valid blocked reading then records by row 1) | **RT7**, F2 |
| 8 | current | reading `reset`, Ow > Ob, Rw > Ob | cleared | RT3 |
| 9 | current | low reading, Ow > Ob, Rw absent or Rw > Ob | cleared | F2, RT3 |
| 10 | current | reading with Ow ≤ Ob, or Rw ≤ Ob (an earlier cycle); a blocked reading with Ow < Ob | kept unchanged | RT3 (a future reading never arrives: RT2) |
| 11 | any | reading `unknown`, stale or `disabled`; no reading; provider `unknown` | kept | F1, F2, F9, RT1 |
| 12 | legacy | any reading that is not blocked | kept: it cannot be ordered; leaves by row 3, 4, 6 or 7 | RT3 |
| 13 | legacy without valid Rb | none of rows 3, 4, 6 | kept; the hook marks it (T10) | class of RT7 |
| 14 | entry that is not an object | any | the step returns the same value, `null` included; since fix round 4 a file holding one is malformed, so no writer gets this far and nothing is written (PM answer 1 of round 4 replaces "kept verbatim") | F4, RT12 |

`pm-set` never applies row 7: it changes what it is told, the next snapshot expires. F3, F5, F7, F8 and RT5, RT8, RT9 are not transitions (classes a, b, d and paths).

### (d) Every CLI entry point and its one error boundary

| Entry | Arguments or edit refused | Config or path refused (`SystemExit(message)` of `codex_backend`, `state_path`) | Lock busy | `OSError` in the transaction | Any other exception | Today |
|---|---|---|---|---|---|---|
| `usage_snapshot.py` (`main:1257`) | exit 2, usage | message, exit 1, nothing read (all three paths resolved before `collect`) | `quota_blocks: {status: error}`, exit 0 | the same | per provider `unknown` (`guarded`); elsewhere one `{"status": "error"}` JSON, exit 1 | catch at `:1297` covers `OSError`, `SystemExit` only; the blocks path is refused differently from the other two |
| `usage_snapshot.py pm-set` (`pm_set:1192`) | exit 2 | message, exit 1, nothing read | one JSON `status: error`, exit 1 | the same, file unchanged, no temp file left | the same | **traceback (RT9)**, also for a failing `lock.open` |
| `statusline.py` (`main:94`) | exit 2 | printed as the line, exit 0 | no lock | printed as the line, exit 0 | the same | already one boundary (`:106`) |
| hook `session_context.py` (`main:491`) | n/a | `not configured` or `refused` line | `unreadable` line | `unreadable` line | `Quota blocks check failed` | boundary at `:493`; gains the lock case |

The boundary of the file is `transact` itself: every exception raised while it opens, waits for or holds the lock becomes its report, so no caller needs a catch of its own. Each CLI has one function that prints one JSON and picks the exit code.

### (e) The same classes, not probed by the retest

- T1 (invalid row timestamp read as missing), T10 (the hook's second parser), T12 (future mtime read as fresh), row 13, and `lock.open` outside every catch: listed above, fixed in this round.
- A writer killed between the temp write and the replace leaves `<file>.<pid>.tmp`: the temp name becomes fixed (one writer holds the lock), so the next commit overwrites it.
- Left as is, disclosed: a writer without the lock (out of contract); a link created between `state_path`'s check and the open (round 1 note).

### Plan (one round)

1. `parse_time` aware-only and folded with `iso_time`; T1, T12; `pm-set` stores UTC.
2. `block_step` with the table above; `updated_blocks` and `planned_pm_edit` call it.
3. `locked`, `read_quota_file`, `transact` as the boundary, `os.replace` commit; the claim scheme and its five tests removed.
4. Hook: `read_quota_file`, the shared parser, the three results. CLI boundaries. Docs: module docstring, `pm-set` help, README, PM_POLICY section 1 (the recording command re-asserts; an offset is required).
5. Checks of the done check. No new configuration key; `LOCK_WAIT_SECONDS` (safety-bound) now also bounds the reader; `CLAIM_SUFFIX` goes.

**Tests per class** (`test_fix3_*` in `tests/test_quota_tooling.py`): (a) a table over the three results (folder missing, missing under the lock, present, lock held past the bound, lock path a link, `PermissionError`, bad bytes); a real second process committing in a loop while the hook reads (never empty, `CREATE_NO_WINDOW`); a writer paused inside its commit while the hook reads; the name exists before and after every replace. (b) one table, every row T1-T12 × {no offset, date only, `+07:00`, `Z`, wrong type, out of range}, and a property test that a text without an offset is always refused. (c) one table, rows 1-14 × legacy or current × provider read or unread, through `block_step` and both callers. (d) one table, each CLI × a fault double at the lock open, the read, the temp write and the replace: one JSON, the exit code, bytes unchanged, no temp left. Mutants: about 9 in `usage_snapshot.py`, 3 in `hooks/session_context.py`.

### Questions (ranked, with proposed answers)

1. **Two of the 96 retest probes are bound to round 2's internals, so "the probe file runs green" cannot be met as written without code shaped for them.** `test_reader_across_two_commits_keeps_block` calls `us.claim_path` and asserts the reader makes exactly the calls `path, claim, path`; its double makes all three fail, so block text is reachable only by a fourth read through another primitive, under any design. `test_pm_set_no_hardlinks_reports_json_failure` patches `os.link`. *Proposed:* accept the contract of (a); the unchanged file must give 94 passed and exactly these 2 failed (log kept); I port the two to the new contract with the same scenario and assertions (a second process commits while the hook reads; the fault double sits on `os.replace` and on the lock open) in `.tmp/T-026/fix3/` and show the diff. Retest 1's `test_unlocked_pm_write_after_last_read_is_preserved` stays the out-of-contract case retest 2 already accepts. If the file must be green literally, I keep round 2's commit and only lock the reader, and the reader probe still fails.
2. **Which `pm-set` call re-records an exhaustion (row 4)?** *Proposed:* any `--block` call that assigns `resets_at`, even to the same value; every other edit is metadata (row 5). The direction is safe: advancing `observed_at` can only keep a block longer.
3. **Snapshot exit code when the blocks file could not be written.** *Proposed:* stay 0 with `quota_blocks.status: error` in the JSON (the readings were produced; wake sessions read the JSON); `pm-set` exits 1.

## Fix round 3 PM approval (2026-11-15; PM decisions pending user review, decision 94)

Plan approved as audited. Answers:

1. **Q1 approved.** The two probes bound to round 2's internals are ported to the new contract (same scenario and assertion), the diff kept in `.tmp/T-026/fix3/`; `test_pm_set_no_hardlinks_reports_json_failure` injects a filesystem `OSError` at the replace. Report 94 green plus the 2 ported green; the original two stay red on the original file, with the reason. Retest 1's unlocked-writer probe is out of contract and listed as such. The independent retest checks that the ports are faithful.
2. **Q2 approved.** Every `pm-set --block` that sets `resets_at`, even to the same value, is a re-assertion: `observed_at` moves to now. Every other edit is metadata and leaves `observed_at` unchanged. The documented recording command is updated.
3. **Q3 changed.** When the blocks file cannot be read or written the snapshot reports `quota_blocks.status: error` **and exits non-zero**: a lost block must never look like success. Every caller of the snapshot is checked and adapted so that none treats the error as "no block"; the caller list goes into the round section. `pm-set` also exits 1.
4. The four extra same-class defects are in scope (bad Codex timestamp read as missing, future `--claude-file` mtime read as fresh, the hook's own time parser, `lock.open` outside every `try`).
5. `LOCK_WAIT_SECONDS` keeps its safety-bound class and also bounds the reader; the 0.2 s override test stays and the reader gets one.
6. Done check as in the dispatch. No Git writes.

## Fix round 3 (retest 2: RT5-RT9; Claude Fable 5.2 `high`, fresh worker, 2026-11-15)

Input: `.tmp/T-026/retest2/review.md`; audit and approval above. R-IDs: R-021, R-015, R-025. Evidence under `.tmp/T-026/fix3/`.

### What changed, by audit class (`tools/agent-workflow/usage_snapshot.py` unless named)

- **(a) Read contract, RT5.** The file keeps one name: a commit writes the fixed temp `<file>.tmp` and `os.replace`s it onto the file (`locked_write`); `CLAIM_SUFFIX`, `claim_path`, `read_current`, `recover_claim`, `put_back` and the `os.link` publish are gone. `locked(path, action)` holds `<file>.lock` (`git_ledger._lock`, bound `LOCK_WAIT_SECONDS`) and is the only way the tool opens the file. `read_quota_file` returns the bytes, or `None` only for a proven absence (no state folder, or missing under the lock), and raises otherwise. The hook (`hooks/session_context.py` `quota_block`) prints the blocks, empty text for a proven absence or a whole file without blocks, and `Quota blocks file ... unreadable: <reason>` for everything else, a busy lock included.
- **(b) Time, RT8.** `parse_time` is the one ISO parser: a text needs its UTC offset, the value is computed against the epoch (no `timestamp()`), `iso_time` is folded in, and the hook's own parser is removed (it calls `parse_time`). `pm-set` stores `resets_at` and `observed_at` in UTC. A Codex row timestamp that is present but not such a time refuses the row (only an absent or null one falls back to the file time); a `--claude-file` dated after now is `unknown`.
- **(c) State machine, RT6 and RT7.** `block_step(key, block, now, reading=None, pm=None)` holds the 14-row table in its docstring; `updated_blocks` and `planned_pm_edit` only call it. A block's own reset is checked for every entry, read provider or not; a `pm-set --block` that assigns `resets_at` moves `observed_at` to now (or to the `observed_at` given), any other edit leaves it. An entry whose provider or window is not text is kept and no longer crashes the merge (it was hashed before).
- **(d) Error boundaries, RT9.** `transact` reports every exception raised while the lock is opened, awaited or held; `quota_file_report` reports a failure before it (state folder cannot be made, path refused at creation). Both commands print exactly one JSON document. **Exit codes:** snapshot 0, or 1 when `quota_blocks.status` is `error` (PM answer 3); `pm-set` 0, or 1 with `status: error`; 2 for refused arguments; a refused configuration or path exits with its message before anything is read (all three quota paths are now checked before `collect`).
- **(e)** Fixed temp name (a killed writer's leftover is overwritten by the next commit); a link at the temp path is refused; the hook treats an object without a `blocks` member as no blocks (what `pm-set --text` alone writes; it printed `unreadable` before).

### Callers of the snapshot (PM answer 3: none may read an error as "no block")

| Caller | Where | What it uses | Adapted |
|---|---|---|---|
| `cli_worker.py` | `:2040`, `:2075`, `:2140`, `:2198` | `cache_warning`, `append_ledger`, `codex_snapshot` + `ledger_usage` (per-turn ledger) | No change needed: it never runs the command and never reads the blocks file or an exit code. |
| SessionStart hook | `hooks/session_context.py:355-378` | `state_path`, `read_quota_file`, `parse_time` | Changed: unreadable is printed, never empty text. |
| `statusline.py` | `:79-90` | `state_path`, `atomic_write_text` for its own files | No change needed. |
| Any hook, `.ps1`, `settings.example.json` | grep over `tools/agent-workflow` | none runs `usage_snapshot.py` as a process (allow rules are phase 2) | Nothing to adapt; phase 2 wake prompts must check the exit code. |
| PM_POLICY section 1 | read line (`:9`), step 1 (`:12`), step 3 (`:18`) | tells the PM to run the snapshot and `pm-set` | `:9` says a non-zero exit means the blocks are unknown; `:12` is the new recording command (offset required, setting `resets_at` re-records); `:18` unchanged. |
| ORCHESTRATION section 2 | `:26` | session-start quota step | Says a non-zero exit or an `unreadable` line means unknown, not empty. |
| README "Codex workers" | `:119` | the tool's documentation | Rewritten for the contract, the times and the exit codes. |
| `CODEX_JOBS.md:13`, `.agents/skills/agent-setup/SKILL.md:18`, N135 `:31` | run or name the snapshot for its readings | say nothing about blocks | Unchanged. |
| Tests | `tests/test_cli_worker_codex.py:2227`, `:2346` | assert `main() == 0` with a writable file | Still correct. |

### Probes (`.tmp/T-026/fix3/`)

- Unchanged retest file: before `probes-before.txt` → `7 failed, 89 passed in 3.70s`; after `probes-after.txt` → `2 failed, 94 passed in 2.84s`. The two red ones are the probes bound to round 2's internals: `test_reader_across_two_commits_keeps_block` (`AttributeError: module 'usage_snapshot' has no attribute 'claim_path'`) and `test_pm_set_no_hardlinks_reports_json_failure` (`assert 0 == 1`: its double patches `os.link`, which no commit calls, so the write succeeds).
- Ported file `test_ported_probes.py` (`ported-probes.diff`, 73 lines, only these two functions and one import): `probes-ported.txt` → `96 passed in 2.30s`. Reader probe: two real commits through `transact` are started in threads at the moment the hook opens the file; asserted: nothing landed inside the read, the hook prints `codex weekly blocked`, both commits land afterwards. Hard-link probe: the same double on `os.replace`; assertions unchanged.
- Out of contract, not kept: retest 1's `test_unlocked_pm_write_after_last_read_is_preserved` (a writer without the lock at the instant of the replace).

### Tests per audit class (`tests/test_quota_tooling.py`, `test_fix3_*`; with `test_session_context.py` 330 cases, all in `final-rerun.txt`)

- (a) `test_fix3_read_contract_of_the_session_start_hook` (16 rows: 4 proven empty, 2 block, 10 unreadable, among them "no file but the lock is held"), `test_fix3_read_quota_file_proves_absence_or_raises`, `..._a_reader_during_a_commit_waits_and_never_sees_no_block`, `..._the_hook_reads_the_block_while_another_process_commits` (a real second process, 150 commits, `CREATE_NO_WINDOW`), `..._a_commit_replaces_the_stable_name_and_never_removes_it`. The reader's lock wait is overridden to 0.2 s in the first three and in the hook fault test (PM answer 5); the writer's 0.2 s test stays.
- (b) `..._a_time_without_a_utc_offset_is_refused_at_every_entry` (11 values × entries T1, T4, T7, T8, T9, T10, T11), `..._a_time_with_any_utc_offset_is_one_moment_at_every_entry` (4 spellings of one moment), `..._a_codex_row_without_a_timestamp_uses_the_file_time`, `..._a_claude_file_dated_after_now_is_not_fresh`, property test `..._parse_time_never_depends_on_the_host_zone`.
- (c) `test_fix3_block_step_table` (32 cases over the 14 table rows), `..._both_writers_follow_the_table` (the same cases through `updated_blocks` and `planned_pm_edit`, 37 with the unread-provider variants), `..._a_pm_re_assertion_outlasts_an_older_reading_and_a_metadata_edit_does_not` (command line), `..._an_entry_with_an_unhashable_name_is_kept_and_still_expires`.
- (d) `..._a_failed_transaction_is_one_error_report` (8 faults × 2 commands: one JSON, exit 1, bytes unchanged, no temp left, no traceback), `..._transact_reports_every_failure_itself` (6 faults), `..._exit_codes_of_both_commands`, `..._the_hook_reports_every_fault_and_never_raises`.
- Earlier tests changed because the rule changed: `test_quota_blocks_owns_only_the_blocks_list` (an expired block of an unread provider is now dropped: RT7); the sharing-violation double writes to the replace's target; the five round 2 claim tests are removed with the claim.

### Mutants (`mutants.py`, scratch copy, removed after; `mutants.txt`, `mutants-rerun.txt`)

20 mutants: `usage_snapshot.py` 16, `hooks/session_context.py` 4. First run 18 killed. Survivors 13 (`transact` catching only a busy lock) and 16 (`quota_file_report` catching only `SystemExit`) showed that the command-level tests could not tell the two boundaries apart; added `test_fix3_transact_reports_every_failure_itself` and the faults "`.tmp` is a file" and "link appears before the folder is made". Rerun: both killed. 20 of 20 killed. `statusline.py` and `codex_backend.py` are unchanged this round.

### Configuration audit (rows added or changed this round)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Lock wait 5 s, now also the reader's bound | `usage_snapshot.LOCK_WAIT_SECONDS` | n/a | n/a | writer: `test_a_held_lock_stops_the_write_and_leaves_the_file` (0.2 s); reader: `test_fix3_read_contract_...[lock held]`, `test_fix3_read_quota_file_proves_absence_or_raises` (0.2 s) | safety-bound (unchanged class) |
| Temp name `<file>.tmp` | `usage_snapshot.TEMP_SUFFIX` | a link there is refused | n/a | `test_fix3_a_commit_replaces_the_stable_name_and_never_removes_it` | artifact-bound |
| Claim name `<file>.claimed` | removed | | | | |
| ISO times need a UTC offset; stored in UTC | none: no setting, a zone is never assumed | `parse_time`; `pm_edit_problem` (exit 2) | PM_POLICY step 1, `pm-set -h` | time table tests (refusals and the four accepted spellings) | protocol-bound (ISO 8601) |
| `observed_at` of a re-recorded block | the call's time unless `--text observed_at` is given | aware, not after now | `pm-set -h` | `test_fix3_block_step_table` rows 4, 5; the command-line test | design (PM answer 2) |
| Exit codes 0, 1, 2 | none | n/a | module docstring, `pm-set -h`, README | `test_fix3_exit_codes_of_both_commands`, the fault table | protocol-bound (command contract, PM answer 3) |

No configuration key was added, removed or changed; `codex_backend.py` and `codex_profiles.json` are untouched.

### Problems, risks and remaining work

- **`.agents/policy/PM_POLICY.md` is over its 23000-character budget: 23770** (22987 at HEAD, 23591 after round 2, +179 this round for the two facts the PM asked for). Condensing other sections or raising the budget is the PM's call; no test fails on it, the session-start hook will report it.
- A writer without the lock is still out of contract: the re-read before the replace narrows the moment, it does not close it.
- A hung lock holder makes the SessionStart hook wait 5 s and print `unreadable`; the hook creates the empty lock file when the state folder exists.
- Blocks without a valid `observed_at` and without a valid `resets_at` stay until `pm-set --remove-block` or a new recording; the hook marks them "no valid reset time".
- The link check precedes the open (round 1 note, unchanged).

### not_run

- `ruff check` / `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed. The modules import in the tests.
- Native Linux: the `flock` branch of the lock and POSIX `rename` semantics; the contract's proof is the lock, the experiment is NTFS only.
- `agy` and `claude` commands; the live `usage_snapshot.py` (it would write this worktree's `.tmp/pm-state/`); retest 1's probe file (its RT1-RT3 classes are in the retest 2 file).

### Noticed, not done

- `usage_snapshot.py` reuses the private `_lock`/`_unlock` of `hooks/git_ledger.py` and `_is_link` of `purge_tmp.py`; a rename there breaks the quota tests first.
- The SessionStart hook loads a fresh copy of `usage_snapshot.py` on every run (`load_module`), so a module-level override does not reach it; the tests set it on that copy.

### Final check (fix round 3)

- Full suite: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests` (footer rule 1 environment, own `--basetemp`; runner `.tmp/T-026/fix3/run.sh`) → `5088 passed, 10 skipped in 772.71s (0:12:52)` (`full-suite.txt`; the skips are the host conditions of round 1).
- That run started before the two mutant-driven test additions, the shortening of two PM_POLICY sentences and this section; the affected tests were run again on the final tree: see the last line.
- Child processes: the new process test and `mutants.py` use `CREATE_NO_WINDOW`; product code starts no process in this round. Scratch removed (`copy`, `mut`, temp folders); kept under `.tmp/T-026/fix3/`: the logs named above, `experiment.py`, `mutants.py`, `run.sh`, the two probe files and the diff.
- Changed files this round: `tools/agent-workflow/usage_snapshot.py`, `tools/agent-workflow/hooks/session_context.py`, `tools/agent-workflow/tests/test_quota_tooling.py`, `tools/agent-workflow/README.md`, `.agents/policy/PM_POLICY.md`, `.agents/ORCHESTRATION.md`, this task file. No Git write.
- Rerun on the final tree, after this section was written: `... pytest ... -rs tools/agent-workflow/tests -k "test_the_real_checkout_has_no_failures or test_apply_binds_a_copy_and_is_idempotent or quota_tooling or session_context or standing_rules or hook_robustness or cli_worker_final"` → `416 passed, 4690 deselected in 44.97s` (`final-rerun.txt`): the quota and hook tests, plus every test file that reads the task file, PM_POLICY or ORCHESTRATION. Later edits to this section changed three counts in its text only.

## Fix round 4 audit (Step 0; Claude Fable 5.2 `high`, fresh worker, 2026-11-15; waiting for approval, no code changed)

Input: `.tmp/T-026/retest3/review.md` (FAIL, RT10-RT13), decision 114, standing rule S31. R-IDs: R-021, R-015, R-025. Evidence gathered: `.tmp/T-026/fix4/probes-before.txt` (the retest's `test_review_fresh.py` unchanged on the round 3 code: `16 failed, 67 passed in 3.64s`, the retest's sixteen) and `.tmp/T-026/fix4/experiment.txt` (script `experiment.py`, scratch copy of the modules, six same-class cases the retest did not probe). Line numbers are those of the round 3 files; `us` is `tools/agent-workflow/usage_snapshot.py`.

### Root cause (one class, source-verified)

The stored format has no owner. Five places decode `quota-blocks.json` (`planned_blocks`, `splice_blocks` with `find_member`, `blocks_count`, `planned_pm_edit`, the hook's `quota_block`) and three places judge the status-line data (`status_observation`, the status line's own `claude_line` / `antigravity_line` / `percent`, its stdin reader). Each one turns "I found nothing I can use" into the value that means "nothing is there": an empty list, an empty document, `None`, empty text, or the no-limit text.

| Finding | Where emptiness stands for absence |
|---|---|
| RT10 | `us.planned_blocks:1032` and `us.planned_pm_edit:1186` test `text.strip(...)` and fall through to the no-file value (`existing = []`, `data = {}`); `us.splice_blocks:874` repeats the rule and builds a new document over the old bytes. |
| RT11 | The hook (`hooks/session_context.py:375-376`) skips an entry that is not an object; with no line left it returns empty text (`:390-391`), the text of a proven absence. |
| RT12 | `us.block_step` uses `None` for "no block" as input and as result (`:949`, `:970`) and `us.updated_blocks:909-910` drops a `None` result; JSON `null` decodes to `None`. |
| RT13 | `statusline.py:56-59` and `:66-74` apply their own shape rule (`percent:46`), skip what fails it and print "no limit window" / "no quota bucket" when nothing is left; the snapshot validates the same member with `us.claude_status_windows:245` and `us.antigravity_windows:312`. |

### (a) Every reader of `quota-blocks.json`

| # | Reader | Code | "Absent" today | Validator result it will use |
|---|---|---|---|---|
| B1 | Snapshot writer | `us.planned_blocks:1023-1045` through `write_quota_blocks`, `transact` | no file; **blank or BOM-only bytes (`:1032`)**; an object without `blocks` (`:1037`) | absent: a document is created only when a block is recorded. Valid: the entries. Malformed: `status: error` with the reason, exit 1, bytes unchanged |
| B2 | Snapshot transitions | `us.updated_blocks:888-915`, `us.block_step:918-981` | **an entry equal to `None` is "no block" (`:949`, `:970`, `:909`)** | object entries take the step; an entry that is not an object, `null` included, is row 14 and keeps its bytes; "no block" becomes a sentinel of its own (`NO_BLOCK`), as input and as result |
| B3 | Snapshot render | `us.splice_blocks:869-881`, `us.find_member:829` | its own blank rule (`:874`); a second parser beside `json.loads`; every entry is rendered again when one changes (experiment 3: `1.50` becomes `1.5`, `{"a":1,"a":2}` becomes `{"a": 2}`) | renders from the validator's spans: an entry the step left as it was is copied as its source text |
| B4 | Report count | `us.blocks_count:1124-1127` | its own `json.loads`; every entry counts as a block | counts from the validated result: `blocks`, and `unreadable_entries` when there are any |
| B5 | `pm-set` | `us.planned_pm_edit:1177-1214` | no file; **blank or BOM-only (`:1186`), then it overwrites the file**; entries that are not objects are passed over (`:1199`) but the whole document is rendered again (experiment 4: `1e2` becomes `100.0`) | as B1; the `blocks` value goes through B3's renderer |
| B6 | SessionStart hook | `quota_block:342-392` after `us.read_quota_file:1072` | a proven absence (kept); an object without `blocks`; **entries that are not objects skipped (`:375`), no line left gives empty text (`:390`)**; its own decode (`:362`) and parse (`:366`) | absent: empty text. Malformed: `unreadable: <reason>`. Valid: one line per block, one `unreadable entry` line per entry that is no block; empty text only for a valid result without entries |
| B7 | Status line | `statusline.py:77-91` | does not open this file | none |
| B8 | `cli_worker.py:2035`, `:2117` | ledger helpers only | does not open this file | none |
| B9 | The PM's Read tool, the user | outside the code | no rule of the tool | the README names the shape and the repair |

Same class in B1 to B6, not probed (experiments 1 and 2): a **duplicated `blocks` member** (both parsers take the last one; the hook printed empty text while the first list held a live block) and **constants that are not JSON** (`NaN`, `Infinity`; Python's decoder accepts them, the hook printed empty text for `[Infinity]`). The validator refuses both as malformed.

### (b) Every reader of the status-line source files, and of the same data before it is stored

| # | Reader | Code | "Absent" today | Validator result it will use |
|---|---|---|---|---|
| S1 | Snapshot | `us.status_observation:549-572`, called at `:639` (Claude) and `:676` (Antigravity) | no file, unreadable, blank, not JSON, no `written_at`, member not an object: one refusal string. Fail-closed when it is the only source, **but beside another source the string is dropped (`:640`, `:678-680`), so a malformed file reads exactly like an absent one** (experiment 6: provider `ok`, no `refused`) | absent: not a source, said only when no source is left. Malformed: listed under `refused` with its reason, like a future-dated reading. Valid: an `Observation`, each window validated |
| S2 | Status line, printed line | `statusline.py:52-74`, `percent:46` | **a window that fails its own rule is skipped; nothing left prints the no-limit text** (RT13; experiment 5: also an object window with a text value); its rule is not the snapshot's (it prints `250% used`, `700% left`) | validates the document it stores with the validator S1 uses and prints from that result: a valid window with its value, a malformed one as `<name> unknown`, the no-limit text only for a valid empty map |
| S3 | Status line, stdin | `statusline.py:99-103`, `:83` | blank input, broken JSON and a top level that is not an object all become `payload = None` and print "no rate_limits yet", the text of a truly absent member | the validator's decoder: an absent or null member prints "no ... yet" (documented: the member appears after the first API response); malformed input prints `input unreadable: <reason>`; nothing is written in either case, as today |
| S4 | `--claude-file` | `us.claude_snapshot:702-721` | missing and not JSON share one reason; `unknown` both ways | the same decoder; the reason says which; the result is unchanged |
| S5 | The hook, `cli_worker.py` | — | do not read these files | none |

Out of this round: Codex session rows and worker turn logs are the providers' own logs, not a stored format of this tool, and already pass `observe` (RT1, RT2 closed).

### The validator (in `usage_snapshot.py`, the module the status line and the hook already load; no new file)

- `decode_object(raw)`: the one decoder. UTF-8, at most one leading BOM, strict JSON (no `NaN` or `Infinity`), exactly one object, nothing after it. Walked once, so there is one parser and each member and entry has its source span.
- `stored_blocks(raw)` and `stored_status(raw, member, parse)`: the two shapes over it. Each returns a `Stored` value whose `tag` is `valid`, `absent` (only for `raw is None`: the caller proved there is no file) or `malformed` with a `reason`. Blank and BOM-only bytes are malformed.
- Valid blocks: the text, the top-level members, and one `Entry` per element of `blocks` (value, span, `problem` when it is not an object). An object without `blocks`, or with an empty list, is the validated empty shape.
- Valid status: `written_at` (a time with its UTC offset) and the windows of the existing per-window validators (`state: unknown` with a reason for a malformed window).
- No consumer decodes these bytes, calls `json.loads` on them, or tests them for emptiness; each branches on `tag`.

### Plan (one round)

1. The validator and its property tests.
2. Writers: `planned_blocks` and `planned_pm_edit` take a `Stored`; `block_step` with `NO_BLOCK`; one renderer of the `blocks` value from spans; `find_member`, `splice_blocks` and `blocks_count` fold into the validator and the renderer.
3. Hook: `quota_block` over `stored_blocks`. Status line: prints from `stored_status` of the bytes it writes; stdin through `decode_object`. Snapshot: `status_observation` over `stored_status`, a malformed file under `refused`.
4. Docs: module and hook docstrings, `pm-set -h`, README (the shape, and that a malformed file or entry is repaired by hand). `PM_POLICY.md` stays untouched (it is over its budget). Configuration audit rows: the stored shape (artifact-bound), strict JSON (protocol-bound); no configuration key is added or changed.
5. Checks of the done check.

**Tests per finding** (`test_fix4_*` in `tests/test_quota_tooling.py`):
- RT10: empty, white space, BOM, BOM plus white space × the snapshot command and `pm-set`: one error JSON, exit 1, bytes unchanged; the hook prints `unreadable`.
- RT11: `[null]`, `[false, 71]`, `["text"]`, `[[]]`, and each beside a valid block: an `unreadable entry` line, and the valid block's line still printed.
- RT12: the same lists with unread providers: bytes unchanged. With a sibling block that expires or is recorded, and with a `pm-set --block` edit: each untouched entry's source text (`null`, `1.50`, `{"a":1,"a":2}`) is still in the file.
- RT13: a window that is not an object, an object window with a wrong type or an out-of-range value, a valid window beside a malformed one, an empty map: the line never claims no limit for a malformed map, and the snapshot reading the stored file gives the same state per window.
- Table: the shapes of the done check (absent, empty, blank, BOM, invalid UTF-8, a top level that is not an object, entries that are not objects, mixed, valid) plus duplicated `blocks` and `NaN`, each through the snapshot command, `pm-set`, the hook (blocks file) and through the status line and the snapshot's provider reading (status-line document and stdin).
- Property: for arbitrary bytes and for generated JSON texts with random damage, `tag == "valid"` implies that an independent strict parse gives one object of the documented shape and that every entry span decodes to its entry; a writer never changes the bytes of a malformed file.
- Mutants: about 8 in `usage_snapshot.py`, 3 in `hooks/session_context.py`, 3 in `statusline.py`.
- Earlier tests that change because the rule changes: `test_statusline_keeps_the_last_reading_when_a_payload_has_none` (rows `""`, `{broken`, `[1]` now print `input unreadable`), the spelling of "no block" in `test_fix3_block_step_table`, the blocks count of a report that holds entries which are no blocks.

### Questions (ranked, with proposed answers)

1. **How far does "kept byte for byte" reach?** Today an untouched entry survives by value only when the file is written (experiments 3 and 4). *Proposed:* strict. When nothing changes, no write. When the snapshot or a `pm-set --block` call changes one entry, every other entry keeps its own source text (spans from the validator, one renderer for both writers); only the white space between entries is laid out again. `pm-set` keeps rendering the top-level PM members as it does now. The narrower choice I dropped: keep values only, which loses duplicate keys and number spellings in entries the tool does not own.
2. **A list that holds an entry which is no block** (for example a truncated one). *Proposed:* both commands keep working on the valid entries, so a new exhaustion is still recorded, and report `unreadable_entries` (index and reason). The snapshot then exits 1 (PM answer 3 of round 3: the recorded blocks are not fully known); `pm-set` exits 0 when its edit landed. No new command: the reason text and the README say such an entry, like a malformed file, is repaired by hand, the one hand edit in contract.
3. **Same-class changes beyond RT10 to RT13:** S3 (malformed stdin no longer prints "no rate_limits yet"), S1 (a malformed status-line file listed under `refused`), a duplicated `blocks` member and `NaN` / `Infinity` refused as malformed. *Proposed:* all in scope, as the one validator decides them anyway; each has a row in the table test.

## Fix round 4 PM approval (2026-11-15; PM decisions pending user review)

Audit, root cause and plan accepted (one validator in `usage_snapshot.py`, three tags, `NO_BLOCK`). Answers:

1. **No byte-level splicing.** A file malformed in any part (top level, any entry, a duplicate member, `NaN` or `Infinity`): both writers write nothing, bytes unchanged, exit 1, one `status: error` report listing the reasons; hand repair is the way out. A valid file is rendered again canonically: the contract is equal values (`1.50` may become `1.5`), documented and tested. Row 14 of the transition table changes from "unchanged bytes" to "no write".
2. **Readers** (hook, status line) show every valid entry plus one diagnostic naming the unreadable part; never empty text, "no block", "no limit" or "no rate_limits yet" for malformed input. Absent stays absent. Broken stdin is malformed.
3. **Scope: yes** for every same-class case reproduced; each is a row of the table test.

## Fix round 4 (retest 3: RT10-RT13; Claude Fable 5.2 `high`, 2026-11-15)

Input: `.tmp/T-026/retest3/review.md`; audit and approval above. R-IDs: R-021, R-015, R-025. Evidence under `.tmp/T-026/fix4/`.

### What changed (`us` is `tools/agent-workflow/usage_snapshot.py`)

- **The validator** (`us`, no new file): `parse_object` is the one parser (strict JSON through `STRICT_JSON`: no `NaN` or `Infinity`, no number beyond a float, no member name twice at any depth; one leading BOM; exactly one object; it records each top-level value's span). `decode_object(bytes)` gives a `Document` or the reason. `stored_blocks(raw)` and `stored_status(raw, member)` return a `Stored` with `tag` VALID, ABSENT (only for `raw is None`) or MALFORMED with `problems`; for the blocks file `Stored.blocks` holds the object entries also beside a malformed one. `find_member` and `splice_blocks` now sit on `parse_object`; `blocks_count` on `stored_blocks`. No consumer decodes these bytes itself any more.
- **Writers** (`planned_blocks`, `planned_pm_edit`): MALFORMED gives `malformed_report` (`status: error`, `reason`, `problems`), nothing written, exit 1 for both commands. VALID: the snapshot renders the `blocks` value again and leaves the bytes outside it (F4 kept); `pm-set` renders the document again (BOM and line ending kept). `pm-set --json` values are decoded strictly and the planned bytes are checked by the validator before the commit, so the tool cannot write a file it then refuses. Removing a block that is not there is `unchanged` (it used to add an empty `blocks` member, and made a file).
- **RT12:** `NO_BLOCK` is the "no block" value of `block_step`, as input and result; a JSON `null` is row 14 like every other entry that is not an object.
- **Hook** (`hooks/session_context.py` `quota_block`): branches on the tag. ABSENT or a VALID file without blocks: empty text. MALFORMED: the blocks it still holds, then one line `Quota blocks file <path> unreadable: <each problem> (...)`.
- **Status line** (`statusline.py`): stdin goes through `decode_object` (`<source>: input unreadable: <reason>`; "no ... yet" only for an absent or null member); the bytes about to be stored go through `stored_status`; the line is built from `us.window_shapes` (the snapshot's window validators, time left out): valid windows with their value, then `unreadable: <name> (<reason>)`, and the no-limit text only for an empty member.
- **Snapshot readers:** `status_reading` returns the tag with the reading; a MALFORMED status-line file is listed under `refused` for Claude (also beside worker logs) and Antigravity (also beside the command); an ABSENT one is not. `--claude-file` uses `decode_object` and says "file missing" or "file is not a JSON object: <reason>".
- **Docs:** module docstrings, `pm-set -h`, README "Codex workers" (the shape, the three results, hand repair, equal values). `PM_POLICY.md` and `ORCHESTRATION.md` untouched.

### Decisions inside the approved plan (for the reviewer)

- The snapshot still replaces only the `blocks` value (existing behaviour, F4 closed by its own test); the value itself is rendered canonically, as PM answer 1 says. `pm-set` renders the whole document, as before.
- `NaN` in a status-line file is a document-level defect (the file is refused whole), not a window-level one; the status line can no longer store one, because its input is decoded strictly. Three rows of the fix 1 tables moved to the round 4 table for that reason.
- The status line judges a window's shape with time left out (`window_shapes` reads it as fresh and before any reset), so an old `reset_time` in the payload does not turn the printed value into 0%; whether a reset passed stays the snapshot's question.
- `status_observation` stays as the reading alone (closed probes and tests call it); the two consumers call `status_reading`.

### Probes (`.tmp/T-026/fix4/`, runner `run.sh`: footer rule 1 environment, through `test_slot.py`)

- The retest's `test_review_fresh.py`, byte-identical to `.tmp/T-026/retest3/` (`cmp`): before `probes-before.txt` → `16 failed, 67 passed in 3.64s`; after `probes-after.txt` → `83 passed in 7.96s`.
- The ported probes of round 3 (`test_ported_probes.py`, unchanged): `ported-after.txt` → `96 passed in 28.07s`. No probe was edited or ported in this round.

### Tests per finding (`tests/test_quota_tooling.py`, `test_fix4_*`)

- **Shape table, blocks file** (34 rows: absent; 5 valid; blank, white space, BOM, two BOMs, invalid UTF-8, UTF-16, broken JSON, four top levels that are no object, text after the object, three `blocks` that are no list, `blocks` twice, another member twice, a member twice inside a block, `NaN`, `Infinity`, a number beyond a float, four lists of entries that are no object, two mixed lists): `..._the_validator_tags_each_shape`, `..._shapes_through_the_snapshot_command` (each row with nothing blocked and with a blocked window), `..._shapes_through_pm_set` (a top-level edit and a block removal), `..._shapes_through_the_session_start_hook`. RT10, RT11, RT12.
- **Shape table, status-line document** (23 rows × Claude and Antigravity): `..._status_shapes_through_the_validator_and_the_snapshot` (alone and beside another source: worker log, or a labelled double of the usage command) and `..._status_shapes_through_the_status_line` (the same bytes as its input; then the snapshot reads what it stored and must give the same state per window). RT13 and the `refused` case.
- `..._every_unreadable_entry_is_listed`, `..._an_entry_that_is_no_object_is_never_no_block` (8 values × 4 events, RT12), `..._no_block_is_its_own_value`, `..._pm_set_writes_a_valid_file_again_with_equal_values` and `..._the_snapshot_renders_the_blocks_value_and_keeps_the_rest` (PM answer 1), `..._pm_set_refuses_a_value_the_file_could_not_hold`, `..._the_claude_file_reason_tells_missing_from_malformed`, `..._the_status_line_prints_each_kind_of_window`.
- **Property tests** (hypothesis, bytes near the documented shape with random damage): `..._property_blocks_are_valid_only_in_the_documented_shape` and `..._property_status_is_valid_only_in_the_documented_shape` (600 examples each): VALID exactly when an independent strict reading with the standard library alone gives the documented shape, in both directions, so a validator that refused everything fails too; each span decodes to its member. `..._property_a_writer_plans_nothing_for_a_malformed_file` (400): an error report with the validator's problems for a malformed file; for a valid one, bytes that are valid again and keep every value they do not own.
- Earlier tests changed because the rule changed: `test_statusline_keeps_the_last_reading_when_a_payload_has_none` (rows `""`, `{broken`, `[1]` now `input unreadable`); three `NaN` / infinity rows left the fix 1 tables for the round 4 table (document-level now); `test_fix3_block_step_table` maps its "none" to `NO_BLOCK`; `test_collect_reports_claude_from_the_pm_file_and_antigravity_unknown` (`tests/test_cli_worker_codex.py`) checks the head of the reason.
- **One round 3 test stabilised, outside RT10 to RT13:** `test_fix3_the_hook_reads_the_block_while_another_process_commits` now gives the reader a lock wait above the writer's whole run (120 s instead of the 5 s default); its assertions are unchanged. Reason and evidence in "Final check" below.
- Quota and hook tests: `own2.txt` → `647 passed in 20.56s` (three runs in a row before that, all 647 passed).

### Mutants (`mutants.py` through `mut.sh`, scratch copy `mut`, removed after; `mutants.txt`)

30 of 30 killed on the first run, no survivor: `usage_snapshot.py` 18 (blank as an empty document, a member twice at the top level and inside a value, `NaN`, a number beyond a float, two BOMs, an entry that is no object left valid, `blocks` that is no list read as empty, `NO_BLOCK = None`, each writer planning a write for a malformed file, a malformed status-line file dropped for Claude and for Antigravity, an absent one listed, a member that is no object valid, `--json` not strict, a window shape depending on the clock, a missing block's removal writing the file), `hooks/session_context.py` 5 (no diagnostic, a diagnostic for every file, valid blocks hidden, parts not named, read without the lock), `statusline.py` 7 (broken input not reported, a non-object input read as an absent member, no-limit text when nothing could be read, unreadable windows left out, a malformed bucket printing its amount, storing what the validator refuses, a disabled bucket unreadable). `codex_backend.py` is unchanged.

### Configuration audit (rows added or changed this round)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Shape of `usage.quota_blocks_file`: one object, optional `blocks`, a list of objects | none: the file's schema, `us.stored_blocks` | the validator itself | README "Codex workers", `pm-set -h` | shape table (34 rows × 4 tests), property test | artifact-bound (the file's schema) |
| Shape of the status-line files: `written_at` with its UTC offset, member `rate_limits` or `quota` an object | none: `us.stored_status`; member names from `statusline.SOURCES` | the validator itself | README, `statusline.py` docstring | status table (23 rows × 2 sources × 2 tests), property test | artifact-bound; member names protocol-bound (unchanged) |
| Strict JSON: no `NaN`, `Infinity`, number beyond a float, repeated member name; one BOM; UTF-8 | none: `us.STRICT_JSON`, `us.parse_object` | n/a | README | table rows, property tests, `test_fix4_pm_set_refuses_a_value_the_file_could_not_hold` | protocol-bound (RFC 8259) |
| Tags `valid`, `absent`, `malformed`; `NO_BLOCK`; `NO_USAGE_VALUE` | none: internal values of `us` | n/a | n/a | `test_fix4_no_block_is_its_own_value`, tables | artifact-bound (internal contract) |
| Exit code 1 and `status: error` with `problems` for a malformed file | none | n/a | module docstring, `pm-set -h`, README | shape tables through both commands | protocol-bound (command contract; PM answer 1 of round 4) |
| Status-line texts `input unreadable`, `unreadable: <name> (<reason>)`, line length 160 | none (`LINE_CHARS` unchanged) | n/a | `statusline.py` docstring | status-line table, `test_fix4_the_status_line_prints_each_kind_of_window` | layout (unchanged class) |

No configuration key was added, removed or changed; `codex_backend.py`, `codex_profiles.json` and `settings.example.json` are untouched this round.

### Problems, risks and remaining work

- **A malformed quota-blocks file now stops both writers until someone repairs it by hand** (PM answer 1): no new block is recorded meanwhile. The snapshot still prints the readings and exits 1, and the hook prints the `unreadable` line at every session start, so it is visible; there is no repair command.
- A file that was accepted before and is refused now: one holding `NaN` / `Infinity`, a repeated member name, or an entry of `blocks` that is not an object. The main checkout's live file was not read by me (outside my folder); if it holds such an entry the first snapshot after integration exits 1 and names it.
- The status line's printed text changed for malformed windows and broken input (see above); a valid payload prints what it printed before, except a disabled Antigravity bucket (`<name> disabled` instead of its percent, as the validator reads it).
- Unchanged from earlier rounds: a writer without the lock is out of contract; the link check precedes the open.

### not_run

- `ruff check` / `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed. The modules import in the tests.
- Native Linux; `agy` and `claude` commands; the live `usage_snapshot.py` and `statusline.py` (they would write this worktree's `.tmp/pm-state/`); the probe files of retests 1 and 2 (their classes are in the ported file).

### Noticed, not done

- **A status-line file read in the clock tick it was written in can be refused as "after now"** (since round 2): `written_at` is rounded to a microsecond, `time.time()` is not, so a snapshot's `now` can lie up to 0.24 µs before it (`.tmp/T-026/fix4/clock.txt`, script `clock.py`: 4495 of 300000 back-to-back reads on Python 3.12.10, win32). The provider is then `unknown` with a `refused` line for that one run (fail-closed). The round 4 tests read one second later. Fix candidates: write `written_at` rounded down, or compare at microsecond precision in `observe`.
- `usage_snapshot.py` still reuses the private `_lock` / `_unlock` of `hooks/git_ledger.py` and `_is_link` of `purge_tmp.py` (round 3 note).
- The hook's ABSENT branch and its "VALID without blocks" case print the same empty text, so a mutant that removes the branch is equivalent; it is kept because it states the contract.

### Final check (fix round 4)

- Full suite: `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests` (footer rule 1 environment, own `--basetemp`; runner `.tmp/T-026/fix4/run.sh`) → `5413 passed, 10 skipped in 810.26s (0:13:30)` (`full-suite.txt`; the skips are the host conditions of round 1). It ran on the final product code. Written afterwards: this section, and the one test change below.
- **A rerun after that failed once, under load.** The rerun of the quota, hook and task-file tests gave `1 failed, 732 passed, 4690 deselected in 181.78s` (`final-rerun-under-load.txt`): `test_fix3_the_hook_reads_the_block_while_another_process_commits`, two reads printing `unreadable: TimeoutError: the lock stayed busy for 5 s`. Cause, measured: file replaces on the machine were slow at that moment (150 commits in one process took 2.9 to 6.6 s, `timing.txt`; about 1 s when the machine is calm), so the writer's 150 commits outlasted the reader's 5 s bound, and a polling reader facing a writer that takes the lock again at once can miss every gap. **It is not caused by this round:** the round 3 test on the round 3 code (this round's files taken from the Git index into a scratch copy, `r3.sh`) failed the same way under the same load (`r3-two-process.txt`: two passes at 6.33 s and 4.03 s, one failure at 14.72 s), and with the machine calm both versions pass (six runs of the working tree at about 0.9 s, three of round 3 at about 1 s). The plan itself costs 27 µs against 13 µs before (`timing.txt`).
- The test now sets the reader's wait above the writer's run; its assertions are unchanged. This is a test-only change the brief did not ask for: the reviewer should judge it. The product behaviour it exposed is the documented one (a busy lock is `unreadable`, never "no block").
- Rerun on the final tree, after that change and this section: `... pytest ... -rs tools/agent-workflow/tests -k "test_the_real_checkout_has_no_failures or test_apply_binds_a_copy_and_is_idempotent or quota_tooling or session_context or standing_rules or hook_robustness or cli_worker_final or test_collect_reports_claude"` → `734 passed, 4689 deselected in 61.25s` (`final-rerun.txt`). The full suite was not run a second time end to end (`not_run`: 14 minutes; the only change after it is that one test and this file, both covered by the rerun). The mutants ran before the test change; it touches no mutated line.
- Child processes: no product code starts a process in this round; `mutants.py` starts its children with `CREATE_NO_WINDOW`; the usage command in the new tests is a labelled double, not a process.
- Scratch removed (`copy`, `mut`, `tmp`); kept under `.tmp/T-026/fix4/`: the logs named above, `experiment.py`, `experiment.txt`, `clock.py`, `clock.txt`, `timing.py`, `timing.sh`, `r3.sh`, `mutants.py`, `run.sh`, `mut.sh`, `exp.sh` and the two probe files.
- Changed files this round: `tools/agent-workflow/usage_snapshot.py`, `tools/agent-workflow/statusline.py`, `tools/agent-workflow/hooks/session_context.py`, `tools/agent-workflow/tests/test_quota_tooling.py`, `tools/agent-workflow/tests/test_cli_worker_codex.py` (one assertion), `tools/agent-workflow/README.md`, this task file (the round 4 sections, and row 14 of the round 3 table as the PM asked). No Git write.

## Fix RT14 (retest 4: the diagnostic must survive truncation; Claude Sonnet 5.5 `high`, 2026-11-15)

R-IDs: R-021. Input: `.tmp/T-026/retest4/review.md`, brief `.tmp/T-026/fix5/task.md`. Evidence under `.tmp/T-026/fix5/`.

**Step 0 (Small: yes, continued).** Unknown: none that blocks. Plan: (1) render `unreadable: <windows>` right after the provider label and never cut it; (2) the valid windows follow in the validator's order (sorted by name, unchanged); (3) over the limit, drop whole valid entries from the end and end with `+N more`; (4) table, property, boundary and mutant tests. Finish: the two RT14 probes show the diagnostic, tests and mutants green. Questions: none; the rule is the PM's (pending user review).

### What changed (`tools/agent-workflow/statusline.py`)

- **Line shape:** `<prefix> unreadable: <bad window> (<reason>), ...; <valid entry>, ..., +N more`. With no malformed window: `<prefix> <valid entry>, ..., +N more`. With no valid window: `<prefix> unreadable: ...` and nothing after it. The empty object still prints "no limit window" / "no quota bucket".
- **`fitted(prefix, diagnostic, shown)`** (new) keeps the longest run of whole valid entries that fits in `LINE_CHARS` together with its `+N more`; the diagnostic is never cut, so when it alone is over the limit no entry is kept (`...; +N more`). An entry is never cut in the middle. `quota_line` calls it.
- **`clip(line)`** (new) is the old 160-character slice, now applied only where a line is not the quota line (`record`'s refusals, `main`'s `input unreadable` and `quota not recorded`). `main` no longer slices the final line, because that slice was what hid the diagnostic. The ASCII step stays last and keeps the length (one `?` per character).
- `LINE_CHARS = 160` and its class (layout, not a deployment choice) are unchanged; no setting was added. Docstring and the comment at `LINE_CHARS` say the new rule.

### Decisions and edge cases (for the reviewer)

- **The reviewer's probe cannot pass as written.** `test_full_cli_keeps_diagnostic_after_many_valid_windows` has two assertions; the first (`unreadable` and `broken` in the line) now passes for both sources. The second (`all(name in line for name in windows)`, all eight bucket names) asks for every entry in 160 characters, which is the "show every entry" reading; the PM rule counts what is dropped instead. Actual line (agy): `agy unreadable: broken (bucket is not an object); bucket-00 63% left, ..., bucket-04 63% left, +3 more`. Run on the unchanged probe file: `2 failed` (the second assertion only). `test_fix5_the_reviewers_probe_shows_the_diagnostic_and_counts_the_rest` is the same input with the PM rule's assertions.
- **One very long valid name** (longer than the limit) is dropped whole: `claude +1 more`. Per the rule an entry is never cut; the value is in the stored file. The alternative (always keep one entry, even over the limit) was not taken because it breaks the width limit the rule keeps.
- **Order is by window name** (the validator's `sorted(member)`, unchanged), not by payload order; the tests sort for the same reason (a first property run found this in the tests, not in the code).
- A bug of my first version, found by the table before any other test ran: a diagnostic with no valid window printed a trailing `; ` (and `+0 more`); `fitted` returns the head alone when there is nothing to list.
- Earlier tests that fixed the old order were changed because the rule changed: `test_fix4_the_status_line_prints_each_kind_of_window` (two expected lines) and the `unreadable` / `shown` split in `test_fix4_status_shapes_through_the_status_line`.

### Tests (`tests/test_quota_tooling.py`, `test_fix5_*`)

- **Table** `..._keeps_the_diagnostic_and_cuts_whole_entries` (14 rows, both sources, through `main`): diagnostic only; 3 valid + 1 broken (fits, no `+N more`); 8 valid + 1 broken (the probe); 40 valid + 3 broken; 8 valid with none; 3 valid with none; one 300-character valid name; one 300-character malformed name (diagnostic over the limit, kept whole). Each row checks: diagnostic equals the named windows with reasons, kept entries are the first ones whole, kept + omitted = total, the `+N more` suffix iff something was dropped, length within the limit unless the diagnostic alone is over it, and maximality (one more entry would not fit).
- **Probe** `..._the_reviewers_probe_shows_the_diagnostic_and_counts_the_rest`; **boundary** `..._fitted_cuts_at_the_limit_by_whole_entries` (160 exactly stays, 161 drops; the cut line with its count at exactly 160 stays, one character more drops the next entry; zero entries kept; empty diagnostic); **non-quota lines** `..._a_line_that_is_not_the_quota_line_is_still_one_short_row` (a refusal naming a 200-character path is clipped to 160).
- **Property** `..._property_a_malformed_window_is_always_rendered` (hypothesis, 300 examples per source, up to 40 windows, random mix of valid and malformed, names up to 45 characters): every malformed window is named whole with its reason and first; valid entries are a prefix with a correct count; within the limit or no entry kept.
- Configuration override and refusal tests: not applicable, no setting changed.

### Mutants (`mutants.py`, scratch copy `mut`, removed after; `mutants.txt`)

12 of 12 killed on the final run (baseline green: `19 passed` for `-k fix5`): separator for a diagnostic-only line removed; loop keeps at least two entries; first fit test strict (`<`); loop fit test strict; count is the kept number; one entry too many kept; fallback count wrong; separator after the diagnostic changed; diagnostic printed after the entries; `clip` does nothing; the quota line cut again by `clip`; the diagnostic dropped when entries are cut. The first run had one survivor (loop fit test strict): the exact-160 boundary row in the `fitted` test was added for it. An earlier run of the mutants was discarded because its baseline had one failing test of mine (a second `clip` assertion whose input could not be long).

### Configuration audit (row changed this round)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Status-line text order and width: `unreadable:` first and never cut, valid entries whole with `+N more`, line length 160 | none (`LINE_CHARS` unchanged, no key added) | n/a | `statusline.py` docstring | table, boundary and property tests above; override and refusal tests not applicable | layout (unchanged class) |

### not_run

- `ruff check` / `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed.
- Native Linux; `agy` and `claude` commands; the live `statusline.py`.

### Noticed, not done

- The README has no text of the status-line line order, so it needed no edit (searched `unreadable:`; no hit).

### Final check (Fix RT14)

- `test_quota_tooling.py` alone: `645 passed` before the last boundary test; after it `-k fix5` gave `19 passed`. `test_quota_tooling.py` on the final tree: `645 passed in 19.71s`. Full tooling suite once, on the final tree (`<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider -rs tools/agent-workflow/tests`, `full-suite.txt`): `5432 passed, 10 skipped in 697.40s (0:11:37)`; the skips are host conditions (POSIX names, FIFO, Git path refusal, bound runtime configuration).
- Changed files: `tools/agent-workflow/statusline.py`, `tools/agent-workflow/tests/test_quota_tooling.py`, this file. No Git write.
