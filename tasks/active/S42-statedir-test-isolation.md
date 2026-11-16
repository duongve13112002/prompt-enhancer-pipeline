# S42-statedir: every tool test gets its own PM state directory

Kind: code. R-IDs: R-021, R-025. Owner: PM (S42, decision 72). Builder: fresh Codex. Reviewer: another family.

## Goal

The tool suite `tools/agent-workflow/tests/` passes in one run, with no deselection and no environment override. No test reads or writes the live `.tmp/pm-state`.

## Step 0

### Task brief

- **Task name:** S42-statedir; requirements R-021 (evidence), R-025 (Windows/Linux).
- **Responsible role:** assigned implementer; no subagents, review, commits or pushes.
- **Task difficulty and assigned model:** nontrivial, not small; fresh Codex builder as dispatched; retain the session configuration.
- **Reasoning effort and reason:** high, per dispatch; state routing crosses imports, fixtures and child processes.
- **Preloaded skills:** config-audit, handoff-report (read).
- **Current status:** BLOCKED after plan approval: the active tool policy refuses all tooling edits, including worktree copies (evidence below).
- **What was already done:** read the task template, skills, project binding, STATUS queue, lessons digest, relevant decision search, primary state resolvers and the requested first test fixtures; searched all tooling Python files and script/config references for state paths.
- **Expected output:** complete class audit, isolated tests, call-time production state resolution where needed, live-state guard, 3-6 scratch mutants, both complete suite runs and completion/handoff sections including `## Worker completion`.
- **Scope:** state-directory readers under `tools/agent-workflow/`, their callers and tests, shared test fixtures, and this task file. Read every affected file and caller before editing it.
- **Out of scope:** `purge_tmp.py`, `tests/test_purge_tmp.py`, PM-owned shared records, unrelated refactors, new configuration keys, network, installs, model/GPU work, live-state contents or mutations, Git writes.
- **May assume:** per-test state may use an English-named child of `tmp_path`; existing explicit scratch CLI paths retain precedence; an unset override retains the documented default; synthetic fixture Git operations performed by existing tests are test behavior, not permission to modify project history.
- **Must ask:** resolve contradictory live-state/test-slot requirements below; obtain Step 0 approval; ask before changing path-policy meaning or introducing a configuration key.
- **Output:** final reply under 12 lines in English; detailed evidence here; no machine paths in tracked text.
- **Step budget:** after approval, approximately 15 audit/read calls, 25 implementation/targeted-check calls, 15 mutation/full-suite/report calls. Stop and report if the same done-check step fails twice or the phase budget is exhausted.
- **Run limit:** bounded CPU work only; reserve up to 30 minutes per full suite run (two runs), plus focused checks. Every pytest/mutant/probe goes through this worktree's `test_slot.py`; use project venv and required environment, local scratch under `.tmp/S42-statedir/`, bounded subprocesses and `CREATE_NO_WINDOW`.
- **Ignored evidence paths:** planned `.tmp/S42-statedir/` scratch and small retained command/mutation reports; no live `.tmp/pm-state` contents are evidence.

### Unknowns and failure modes

1. The required limiter writes live state before pytest fixtures can run. `test_slot.py:18,25,50-66` hardcodes `<project>/.tmp/pm-state/test-slots`, reads its limit/locks, creates a lock and may reclaim one. Running it now would violate the brief's explicit live-state restriction. No tests or probes have been run.
2. An autouse function fixture runs after collection. Modules imported during collection retain cached paths; changing only the environment cannot fix them. Derived paths and default function arguments must be audited too.
3. Some tests intentionally seed `<scratch root>/.tmp/pm-state` and expect that exact default. A global override redirects a dependent module while their seed stays elsewhere; child processes can inherit the same mismatch.
4. Listing/mtime snapshots can detect mutations but cannot prove absence of reads, and unrelated PM activity can change the live directory. The guard needs an access check as well as metadata evidence; metadata-only inspection is explicitly allowed by the brief.
5. Existing fixtures patch public module constants. Changing state resolution must update those seams and all callers without weakening ledger checks. Configured artifact paths (quota/policy/guard files) must be distinguished from the shared state-root rule before any semantic change.
6. The shared fixture can affect the other worker's purge tests. Those files stay untouched; run and report their result after the fixture change.
7. This environment has no `rg`, Read, Grep or Glob tool. Scoped PowerShell searches are the available fallback; one malformed search formatting expression was blocked and corrected. No restriction on live state or secrets was bypassed.

### Questions (ranked; proposed answers)

1. **Required before execution: how should the limiter run without touching live PM state?** Proposed answer: PM explicitly exempts only the outer limiter's existing lock bookkeeping from the no-live-state rule, preserving the machine-wide concurrency limit; pytest and every process launched by tests remain isolated. Alternative: PM approves a scratch limiter directory and explains how to preserve the shared concurrency cap. Do not silently create a private limiter or run the current one.
2. **Guard coverage:** approve an in-process access guard plus a before/after metadata-only live-state check, with scratch-only self-tests and audited child-process state routing. Proposed answer: yes; do not claim a metadata snapshot detects read-only access or that an in-process Python guard covers arbitrary child processes.
3. **Existing path contracts:** should explicit profile-relative artifact paths remain root-relative rather than be reinterpreted by the environment override? Proposed answer: preserve explicit configured paths and containment checks; make implicit shared-state defaults resolve at call time. Escalate any concrete contract conflict found in the full audit.

### Research required before code

Pure refactor of project-owned tooling; no external API, algorithm, inference or provider behavior changes. Primary evidence needed: every state consumer and its callers, fixture setup/import order and subprocess environment construction. If using Python audit hooks or pytest collection hooks, read their installed implementation/documentation before relying on their coverage. Complete the per-test/fixture class audit before editing runtime code.

### Research evidence (Step 0)

| Question | Source | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| What failure is assigned? | `tasks/STATUS.md:75` | Raw | Queue records 42 live-ledger failures and 26 failures under a blanket override; this is supplied evidence, not a reproduced result. | documented |
| Does the launcher resolve all state dynamically? | `tools/agent-workflow/cli_worker.py:47`, `:52`, `:255`, `:632` | Raw | `state_root()` is dynamic; registry/lock/inbox/worker-ledger constants capture it at import; lint waiver uses the dynamic resolver. | source-verified |
| How does the live ledger leak into the Codex tests? | `tools/agent-workflow/hooks/git_ledger.py:75`, `:293`; `hooks/guard_config.py:15`; `tests/test_cli_worker_codex.py:70`; `tests/test_codex_hooks.py:19` | Raw | `snapshot()` fingerprints `ledger_dir(gc.ROOT)`; World patches launcher ROOT/registry/lock/worker-ledger but not the state environment. Codex-hook tests reuse World. | source-verified |
| What is the existing dynamic pattern? | `tools/agent-workflow/purge_tmp.py:218` | Raw | `_state_dir()` reads the override or PROJECT default at call time; this file is read-only for this task. | source-verified |
| Why does a blanket override conflict with seeded tests? | `tests/test_brief_lint.py:643`, `:670`, `:768`; `tests/test_pm_supervisor.py:27`, `:69`; `tests/test_session_context.py:18` | Raw | Fixtures seed scratch default paths or patch module constants; supervisor children inherit the environment. | source-verified |
| Is isolation already shared? | `tools/agent-workflow/tests/conftest.py:19`, `:76` | Raw | Existing shared setup covers Hypothesis and a fixture Git repository; no autouse PM-state isolation. | source-verified |
| Can the mandated runner avoid live state now? | `tools/agent-workflow/test_slot.py:18`, `:49`; `tests/test_test_slot.py:22` | Raw | Runner hardcodes live slots; its own tests avoid them by copying the script into a scratch project. | source-verified |
| Was recall consulted safely? | Commands below | Raw output | Scratch DB build and two searches succeeded; live recall DB untouched. | experiment |

### Initial production-reader inventory

This is the search inventory for planning, not the completed class audit. Expand it to every caller, derived path, test and fixture during plan step 1; distinguish an actual access from a diagnostic string or path-validator input.

| Reader | Current state selection | Timing / follow-up |
|---|---|---|
| `cli_worker.py` | Override or ROOT default | `state_root`/`state_dir` call-time; STATE/REGISTRY/LOCK/INBOX/LEDGER import-time; inspect settings cache too. |
| `hooks/git_ledger.py` | Override or supplied root default | Call-time; snapshot supplies guard-config root. |
| `purge_tmp.py` | Override or PROJECT default | Call-time; do not edit. |
| `recall.py`, `supervisor_events.py` | Override or ROOT default | Import-time STATE; CLI defaults derive from it. |
| `hooks/ctx_nudge.py` | Override or module-location root | Import-time STATE_DIR and derived DEFAULT_STATE/DEFAULT_INBOX. |
| `hooks/process_guard.py` | Override or module-location root | Import-time STATE_DIR/LOG. |
| `hooks/tool_monitor.py` | process_guard state | Import-time CRASH_LOG; warned path reads pg.STATE_DIR. |
| `hooks/precompact_snapshot.py` | Override or ROOT default | Import-time STATE/NOTE. |
| `hooks/session_context.py`, `hooks/standing_rules.py` | Override or ROOT default | Import-time STATE. |
| `hooks/pm_invariants.py` | CLI state argument, override, root default | Call-time. |
| `test_slot.py` | ROOT default only | Import-time SLOTS; blocking question 1. |
| `workflow_metrics.py` | ROOT default only | Import-time REGISTRY. |
| `setup_project.py` | Explicit root/default layout | Call-time fixed root paths for Codex local profiles, state check/create; classify setup-layout contract before editing. |
| `run_approval_gate.py` | Explicit approvals argument or root default | Call-time; classify explicit path contract. |
| `codex_backend.py`, `usage_snapshot.py`, `statusline.py` | Explicit profile artifact paths rooted in project | Structural state-path validation and configured writes; inspect rather than automatically rebase. |
| `hooks/tool_health.py`, `hooks/tool_guard.py`, push/question guard consumers | Configured guard artifact directories / ledger resolver | Audit configured-path callers and defaults; `.tmp/pm-state` also occurs in pure command-parsing fixtures. |
| `pm-supervisor.ps1` | Script project root default | Script startup state path; launcher children may inherit a conflicting override. |

### Configuration impact (before runtime edits)

No new key. These are tooling settings, not enhancer profiles; `.agents/project.json` declares `python.runtime_config_module: null`.

| Affected behavior | Existing source | Planned validation / classification |
|---|---|---|
| Implicit PM state root | `AGENT_WORKFLOW_STATE_DIR`; per-tool project-root fallback | Preserve unset/empty fallback; test two successive non-default overrides after import and the scratch-root default; configurable. |
| Derived registries, locks, inboxes, ledgers, notes and logs | Existing artifact names beneath state | Verify every affected reader follows the same current root; artifact-bound names. |
| Explicit CLI/profile artifact paths | Existing parser/profile and guard configuration | Preserve precedence and existing invalid/outside-path refusals; no new path policy inferred. |
| Test-owned state | Per-test `tmp_path`; autouse fixture and explicit fixture roots | Seed where needed; ensure fixture/child environments agree; test-only. |
| Test limiter location/concurrency/lifecycle | `test_slot.py`, shared slots/max, existing lock protocol | No change pending question 1; preserve global cap and clean up owned locks only. |
| Live-state access guard | Test setup/access checks and metadata snapshot | Never open contents, write or delete live state; no following directory links; safety-bound/test-only. |

Override omission is allowed by the existing state-path contract, so a new missing-key refusal would change the requested behavior. Retain existing invalid explicit-path refusals and test the guard's refusal of live-state access. Document any reader without an existing invalid-override contract instead of inventing one.

### Plan and checkable finish

1. **In progress: complete the class audit before code.** Read remaining consumers/callers and enumerate every state-touching test/fixture by file, symbol and line. Record empty/seeded/default-rule needs and current isolation mechanism, including import-time constants, default arguments, subprocess inheritance and configured artifact paths.
2. **Pending approval: implement isolation and dynamic resolution.** Add per-test state setup before dependent fixtures; align scratch-root fixtures and child environments; convert cached implicit production paths to call-time reads and update their callers. Keep default-rule checks on scratch roots. Do not touch either purge file.
3. **Add meaningful regression checks.** Verify every changed reader with post-import override switches and unset default on a scratch root. Add the agreed live-state guard and scratch-only guard self-tests. Run the named targeted files including purge; run lint and `ruff format --check`, recording exact outputs.
4. **Mutate, run both full suites, report.** Make 3-6 relevant mutants in a scratch copy (disable per-test isolation, freeze state at import, ignore override, route a derived path to the default, disable the guard); run through the approved limiter and report killed/survived/not_run honestly. Run `tools/agent-workflow/tests/` once without an external state override and once with a scratch override, no deselection, sufficient time bound. Record guard evidence, clean scratch/processes and fill Completion report, Configuration audit, Handoff and Worker completion.

**Test sets:** the complete tool suite and all files named in the brief; no parser/validator property tests are newly required because no parser/validator changes are planned (existing property tests still run in both suites). Configuration checks cover dynamic override, unset default and existing refusal behavior; no new key. Windows is available here; Linux execution is `not_run` locally and belongs to CI.

### Step 0 commands and real output

The project venv Python ran with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` and worktree `src` as `PYTHONPATH`. The executable is the project binding's shared venv, since this worktree has no `.venv`.

```text
<venv python> -B -I tools/agent-workflow/recall.py --help
usage: recall.py [-h] {build,search} ...

<venv python> -B -I tools/agent-workflow/recall.py build --db .tmp/S42-statedir/recall/recall.sqlite
indexed=92 unchanged=0 removed=0 refused=0 chunks=598 skipped_chunks=0

<venv python> -B -I tools/agent-workflow/recall.py search "state directory" --db .tmp/S42-statedir/recall/recall.sqlite --limit 6
3 hits in tasks/archive/T-006-race-followup.md (lines 347, 159, 255); no relevant settled state-isolation decision.

<venv python> -B -I tools/agent-workflow/recall.py search "S42" --db .tmp/S42-statedir/recall/recall.sqlite --limit 3
tasks/STATUS.md:50 [section] - **S42 Antigravity runner: accepted and pushed.**
tasks/archive/status-history-2026-11-14.md:82 [section] Moved 2026-11-15 22:00 (T-022 accepted, T-007 race note, S42 test_slot item, answered question 7)
tasks/STATUS.md:92 [section] - **Closed S42 fixes (stdin briefs, contract-drift docs):** in [status-history](archive/status-history-2026-11-14.md). The Gemini implementation smoke waits on...
```

## Approved implementation record

PM approved the plan and questions 1-3 in the implementation dispatch. Only the outer limiter's existing `test-slots` lock protocol is exempt from live-state isolation. Preserve `test_slot.py`, explicit configured paths and purge files. Guard coverage is limited to in-process access plus metadata changes; child environments are routed explicitly. Ruff is unavailable and installs are prohibited: lint/format `not_run`.

### Class audit before code

Static inventory command: `<venv python> -B -I .tmp/S42-statedir/source_audit.py`.
Output: `source audit: 95 Python files; 1074 test/helper scopes; report=.tmp/S42-statedir/class-audit.md`.
The symbol/line inventory (`.tmp/S42-statedir/class-audit.md`, scratch, removed at handoff) conservatively includes transitive local fixture/helper users and pure assertions sharing tool fixtures; this table classifies their state requirements and current mechanism. Production readers and timing are inventoried above. Configured artifact paths are explicit contracts and remain root-relative; setup checks use the explicitly supplied project layout. The limiter is the approved exception and remains unchanged.

| Tests / fixtures (all dependent scopes indexed in report) | Required state | Current mechanism / defect |
|---|---|---|
| `test_cli_worker_codex.World`, `world`; `test_codex_hooks.world`, `admitted` | Empty, then seeded registry/ledger/policy per test | Scratch root and launcher constant patches; Git snapshot still consults guard-config root without environment routing. |
| `test_cli_worker`, `test_cli_worker_live`, `test_cli_worker_identity`, `test_cli_worker_final`, `test_guard_settings`, `test_clean_checkout` | Empty/seeded registry and locks; final contains default-rule checks | Patch selected constants; other implicit readers remain ambient. Final default-rule assertion must patch a scratch ROOT. |
| `test_cli_worker_git_snapshot`, `test_git_ledger`, `test_git_ledger_final` | Empty then deliberately seeded Git ledger | Most explicitly set override; default-rule branches unset it against fixture roots; audit module-global seams too. |
| `test_brief_lint.cw` and all `send`/waiver tests | Seeded scratch registry, empty scratch waiver log | Patch ROOT and REGISTRY; dynamic waiver resolver inherits conflicting external override. |
| `test_pm_supervisor.tree`, `start`, all supervisor tests | Seeded scratch default layout | Copies scripts under tmp_path; child Python inherits conflicting override while PowerShell uses scratch default. Fix test environment only. |
| `test_session_context.hook`, `tmp_config`; session-context cases in `test_hook_robustness`, `test_quota_tooling` | Seeded scratch notes/registry; cleanup ownership must match | Patch STATE/ROOT; dynamically loaded purge reader still sees ambient override. |
| `test_precompact_snapshot.hook`, `test_hook_robustness.make_precompact` | Seeded notes/jobs/registry or empty state | Patch STATE/NOTE; cached constants prevent post-import environment changes. |
| `test_recall.recall`, `tree`; `test_standing_rules.hook` | Seeded docs and empty/seeded state | Patch ROOT/STATE. Call-time refactor must preserve these explicit patches or update all callers. |
| `test_ctx_nudge` helpers and parallel hook children | Empty/seeded state and inbox | Explicit CLI scratch directories; environment should remain isolated too. |
| `test_process_guard` subprocess helper; `test_process_guard_consumers` | Empty logs, seeded process/guard fixtures | Explicit scratch environment for subprocesses; imported LOG/STATE_DIR can retain ambient paths. |
| `test_tool_guard`, `test_tool_monitor`, `test_push_guard`, `test_hook_robustness` guard cases | Empty logs and seeded configured health/marker paths | Explicit fixture roots/config, some constant patches; derived crash/log paths cache imported process-guard state. |
| `test_final_guard.repo`, default and override checks | Seeded scratch default or explicit override | Fixture unsets environment and passes scratch root; safe default-rule pattern. |
| `test_pm_invariants.world`, `state`, `hook` | Seeded scratch state; explicit environment/flag precedence | In-process explicit state; child helper removes ambient state variable and supplies scratch root. |
| `test_purge_tmp.pt` and related fixtures/property cases | Seeded scratch project default; explicit override checks | Fixture patches PROJECT and unsets override; compatible with an earlier shared autouse fixture. Files remain untouched. |
| `test_quota_tooling` root/profile/session fixtures, `test_trusted_state`, `test_codex_hook_probe` | Seeded explicit profile/config artifact paths | Scratch root/profile containment; preserve configured paths rather than rebase them. |
| `test_run_approval_gate`, `test_setup_project`, `test_prepush_check` | Seeded/absent scratch project layout | Explicit project root and local profile/approval paths; no live implicit-state default required. |
| `test_question_guard`, `test_write_guard_nested`, guard path tables | Seeded configured scratch directories or pure path inputs | Explicit fixture roots; strings naming PM state are not filesystem access by themselves. |
| `test_test_slot._isolated_script` | Empty scratch default slot directory | Copies runner into scratch project; never use real live slots from a test child. Preserve runner behavior. |
| `test_workflow_metrics.jobs`, clean-checkout metrics case | Synthetic transcript/jobs; empty registry when CLI exercised | JOBS/root patches; import-time implicit REGISTRY needs dynamic resolver. |
| `tests/conftest.guard_repo` | Session-owned synthetic repository; per-test logs separate | No shared PM-state fixture currently; add one function-scoped environment override before dependent fixtures. |

### Steps

1. Completed source class audit and documented state classes above (R-021/R-025).
   Exact-symbol inventory retained in `.tmp/S42-statedir/class-audit.md`; static scanner is removed at handoff.
2. Prepared an unapplied refactor draft resolving implicit state at call time while retaining patched constants as explicit overrides.
   Applying the draft was refused before any repository code edit: `Command blocked by PreToolUse hook: hook/launcher files are protected.` Discarded the untested draft/generator; do not infer implementation or a test result from it.

### Implementation blocker

The PM's approval resolves Step 0. The remaining blocker is the worker's active tool policy, not missing user authorization. A single `apply_patch` targeting tooling production readers and their dependent tests was rejected with:

```text
Command blocked by PreToolUse hook: hook/launcher files are protected.
```

Source verification: `tools/agent-workflow/hooks/codex_guard.py:96-105` explicitly says "Worktree copies are protected too" and refuses targets whose first two relative components are `tools`, `agent-workflow`. Therefore absolute-path retries, another writer, a scratch integration trick or changing the guard would bypass the active policy and were not attempted. The guard also rejected the optional Node tool as outside the role allowlist; source inspection continued through the permitted command tool. No subagent was created.

The class audit also found `test_tool_monitor.py:320` calling `th.state_dir(CFG)` with the production default root before checking a session flag. The implementing worker should pass a scratch root there; this is an explicit configured path, so moving that path in production would contradict the approved contract. `tool_health.session_start` itself only probes the shell and does not write state (`hooks/tool_health.py:213-232`).

## Completion report

- **Work completed:** Step 0, approved decisions, class audit and exact-symbol source inventory recorded. This task file is the only repository file edited; no runtime or test change was applied.
- **Result and evidence:** source-verified findings, scratch recall results and static inventory output above. Runtime patch rejected by the active PreToolUse guard; exact reason above. Source inspection is not runtime validation.
- **Important changes and reasons:** preserved the PM's limiter exception and explicit-path contracts in the plan. Did not retry a prohibited tooling write through another mechanism.
- **Problems, risks and remaining work:** dispatch implementation to a worker whose tool policy permits `tools/agent-workflow/` edits. Per-test fixture, dynamic readers, child routing, access/metadata guard, configuration override/default/refusal checks, 3-6 mutants and both complete suite runs are `not_run` because required edits are prohibited. Ruff lint and `ruff format --check` are additionally `not_run` because Ruff is absent and installs are forbidden. Linux checks are `not_run` locally. No completed implementation is claimed.

### Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Implicit state root and derived artifacts | Existing `AGENT_WORKFLOW_STATE_DIR` and tool root/default | No applied runtime edit; implementation prohibited | Existing tooling contracts unchanged | `not_run`: required tooling edit refused; unset default remains permitted by contract | Configurable / artifact-bound (unchanged) |
| Test state, child routing and guard | Planned per-test `tmp_path`, explicit child env, live-state guard | Not implemented | No new key or profile | `not_run`: guard and isolation changes prohibited | Test-only / safety-bound (planned only) |
| Limiter location/concurrency | Existing `test_slot.py` protocol | Unchanged, PM-authorized exception recorded | Existing live `test-slots` subtree only | `not_run`: no test command launched | Configurable / artifact-bound (unchanged) |

Audit is incomplete for acceptance because no implementation or validation could run. Configuration impact above remains the implementing worker's checklist.

## Handoff

- **Current status:** BLOCKED by active tooling-write policy; Step 0 is approved.
- **What was already done:** root-cause trace, production timing inventory, test/fixture class table, exact-symbol static inventory, Configuration impact and approved PM decisions; no live-state contents accessed.
- **What the next agent must do:** use a worker authorized by its tool policy to edit tooling, then implement steps 2-4 and complete validation. Re-read the audit's relevant source lines before code (R-021).
- **What must not be repeated:** no additional Step 0 approval needed; retain PM's answers. Do not retry protected writes through alternate tools or paths. Do not read live state contents or edit either purge file. Exempt only the runner's existing `test-slots` protocol.
- **Worktree, base and partial patch:** assigned S42-statedir worktree; base commit not queried; no partial implementation patch or Git writes.
- **Decisions and notes for the PM to merge:** PM implementation dispatch approved all three proposed answers, pending user review as stated there. Do not change the shared decision/status files from this worker.
- **Lessons learned:** a tooling builder needs a role policy that permits tooling edits; approval in a brief does not alter the active guard. Check this binding before dispatching another implementation turn.

## Worker completion

- [x] Changes: Approved plan, source evidence, class audit, exact-symbol inventory, Configuration impact and policy blocker recorded; no runtime/test code changed.
- [x] Checks: Source inspection, scratch recall build/search and static inventory completed with recorded output. Tests and mutants are `not_run` because tooling edits were refused; Ruff lint/format are `not_run` because Ruff is absent.
- [x] Cleanup: Scratch recall database already removed. Iteration scripts and untested draft removed at handoff; class-audit report retained. No background process, model or server started.
- [x] Remaining: Re-dispatch to a worker permitted to edit tooling; implement isolation, dynamic readers and guard, run 3-6 mutants and both complete suites, then finish Configuration audit. Task is incomplete; approval is already recorded.

## Claude builder

**Status: builder 1 PAUSED (PM stop order: the machine restarts); builder 2 finished, see `### Builder 2`.** Worktree changes are left in place; nothing was reverted. No process of mine is running (pytest PIDs 26040/27296 and test_slot PIDs 30816/8064 were stopped by PID; the worktree's `.tmp/pm-state/test-slots` may hold one stale lock, which the limiter reclaims by liveness). Basetemps removed; no logs kept. Ruff is `not_run` (absent). Linux is `not_run` locally.

### Done

1. New `tools/agent-workflow/tests/live_state_guard.py`: in-process access guard (`sys.addaudithook` on path-taking events: open, mkdir, remove, rename, scandir, listdir, rmtree, copy*, move, sqlite3.connect; raises `LiveStateAccess(PermissionError)` and records the violation). Protected roots: `<project>/.tmp/pm-state` plus the ambient `AGENT_WORKFLOW_STATE_DIR` at import. One exemption: `<project>/.tmp/pm-state/test-slots` (stated in the docstring and the message). Also a metadata-only `snapshot`/`changes` (scandir + lstat, no content, no link following). The docstring states the limits: no child coverage, the snapshot sees writes but not reads, `os.stat` is not an audited event.
2. `tests/conftest.py`: installs the guard at import. A session-start/finish metadata snapshot is reported in the terminal summary (reported, not failed, because other sessions may change the live directory: PM decision pending user review). The autouse function fixture `pm_state_dir` sets `AGENT_WORKFLOW_STATE_DIR` to a fresh empty `tmp_path_factory.mktemp("pm-state")` per test, so every child inherits it. It fails the test at teardown if the guard recorded a violation, even when the code under test swallowed the error.

### Tested (real output)

`.tmp/S42-statedir/run.sh` = `test_slot.py -- <venv python> -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=.tmp/S42-statedir/<name>` (no xdist installed, so no `-n`):

```text
test_cli_worker_codex.py + test_codex_hooks.py:
530 passed, 1 skipped in 154.01s (0:02:34)
live PM state: no metadata change during the run (test-slots excluded)
```

Caveat: this worktree has no live ledger, so these files also passed before the change here. The guard is what proves isolation (an `open` of the ledger path is refused even when the file is absent). A full-suite probe run was started and killed by the stop order before producing output.

### Remaining (next step first)

1. Run the full suite once with the guard to list (a) tests that fail under the blanket per-test override (expected about 26: fixtures that seed `<scratch root>/.tmp/pm-state` and patch module constants: `test_brief_lint.cw`, `test_pm_supervisor.tree/start`, `test_session_context`, default-rule checks in `test_cli_worker*`; `test_purge_tmp` is untouchable and already `delenv`s) and (b) guard violations. Fix (a) with `monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)` in those fixtures (the pattern of `test_final_guard.py:30`), never by touching the purge files.
2. Convert only the readers that violations show leaking to call-time resolution. Candidates: import-time `STATE`/`REGISTRY`/`STATE_DIR` constants in `recall.py`, `supervisor_events.py`, `hooks/session_context.py`, `standing_rules.py`, `precompact_snapshot.py`, `ctx_nudge.py`, `process_guard.py`, `workflow_metrics.py`. Keep the module names patchable (tests patch `STATE`, `NOTE`, `REGISTRY`, `STATE_DIR`, `CRASH_LOG`: grep each before changing). `test_slot.py` stays unchanged. `test_tool_monitor.py:320` passes the production default root to `th.state_dir(CFG)`: pass a scratch root there.
3. `pm-supervisor.ps1` / `test_pm_supervisor.py`: the child Python inherits the override while the script uses its scratch default. Fix the test environment; change the script only if the audit shows a production defect.
4. Guard self-tests (new file, scratch-only): live path refused and recorded, `test-slots` allowed, outside path allowed, snapshot/changes on a scratch tree, child env routing (a child started with the default environment sees the per-test directory), override read at call time and default when unset for each converted reader.
5. 3-6 mutants (disable the autouse fixture, make the guard a no-op, drop the exemption, freeze a converted reader at import, ignore the override). Then the full suite once with no override and once with a scratch `AGENT_WORKFLOW_STATE_DIR`, a fixed `--basetemp` per run under `.tmp/S42-statedir/`, deleted after. Then the Completion report, the row-by-row Configuration audit and `## Worker completion`.

### Builder 2

Re-checked against the source (R-021): the guard and conftest are as the paused builder described (`live_state_guard.py`, `conftest.py` `pm_state_dir`); the limiter's `test-slots` is the only exemption; `test_slot.py` is unchanged.

**Class audit result (one probe run of the whole suite with the guard and the per-test override, before any fix; log `full1.log`):** 27 failed, 2 errors, 5897 passed in 23:54.

| Class | Tests | Cause | Fix |
|---|---|---|---|
| Default-rule fixture reads `<scratch root>/.tmp/pm-state` | `test_brief_lint.py` (18, fixture `cw`), `test_cli_worker_final.py::test_a_crashing_brief_lint_refuses_the_launch` (1) | The per-test override moved the dynamic waiver-log resolver away from the scratch default they seed and read | `monkeypatch.delenv` in `cw` and in that test (pattern of `test_final_guard.py`) |
| Child process uses the script's default state | `test_pm_supervisor.py` (6, helper `start`) | `pm-supervisor.ps1` keeps state under `<tree>/.tmp/pm-state`; its child `cli_worker.py` inherited the override and read another registry, so no event came and the 90 s wait expired | `start` drops the override from the child environment. The script is unchanged: its default rule is the contract, not a defect |
| Seeded scratch state read by a dynamically loaded reader | `test_session_context.py::test_tmp_line_marks_orphans_unknown...` (1) | The purge reader the hook loads resolves state at call time and saw an empty override | `hook` fixture sets the override to the fixture's own state directory |
| Guard violation: live listing and read of the real checkout | `test_setup_project.py::test_the_real_checkout_has_no_failures` (error at teardown) | `check_clis` lists and reads the machine's `.tmp/pm-state/codex.local*.json` | The test stubs `check_clis`, which yields OK and WARN rows only (`setup_project.py:222-258`), while the test asserts FAIL rows only |
| Guard violation: live health directory | `test_tool_monitor.py::test_session_start_hook_with_the_real_event_shape` (error at teardown) | `th.state_dir(CFG)` used the production root | `th.state_dir(CFG, tmp_path)`; `session_start` writes no state (`hooks/tool_health.py:213-232`), so the child leaves no flag |
| Dead link | `test_setup_project.py::test_apply_binds_a_copy_and_is_idempotent` | This task file linked the scratch inventory | The link became plain text |

No production reader leaked: the guard recorded no violation from any reader (violations: only the two test-side ones above). Everything else in the audit table (`test_cli_worker_codex.py`, `test_codex_hooks.py`, `test_git_ledger*`, `test_final_guard.py`, `test_purge_tmp.py` and others) passes under the per-test override without change.

**Decision (PM decision pending user review):** no production reader was converted to call-time resolution. The suite shows no test reaching live state through an import-time constant, and each hook runs as a process that resolves its state at start, which equals call time. Converting ten modules (`recall`, `supervisor_events`, `session_context`, `standing_rules`, `precompact_snapshot`, `ctx_nudge`, `process_guard`, `tool_monitor`, `workflow_metrics`) would touch many patched names for no observed leak; this is the conservative, reversible choice. The override and default behaviour of the import-time readers is covered by child-process tests instead (below). Call-time readers (`cli_worker.state_root`, `git_ledger.ledger_dir`, `purge_tmp._state_dir`, `pm_invariants` main) already follow the override and keep their own tests.

**New tests** `tests/test_live_state_guard.py` (scratch only; 20 cases: 5 guard, 2 snapshot, 2 routing, 10 parametrized readers, 1 end to end):
- guard: live path refused and recorded even when swallowed; only `test-slots` exempt (sibling prefix is not); scratch path allowed; ambient override protected; non-path arguments ignored;
- snapshot: sees added, changed and removed, skips `test-slots`, empty for a missing directory;
- routing: each test has its own empty state directory; a child started with the default environment inherits it;
- readers: each import-time reader follows the override (two different values) and uses `<root>/.tmp/pm-state` when it is unset or empty;
- end to end: a child pytest through the real `conftest` shows that a test which swallows a live access still gets a teardown error (`2 passed, 1 error`).

**Tested (real output).** Commands go through `test_slot.py` with `-X faulthandler`, `--basetemp` under `.tmp/S42-statedir/`, no xdist:
```text
test_live_state_guard + test_setup_project + test_tool_monitor + test_session_context + test_cli_worker_final:
1 failed, 138 passed in 42.75s   (the failure was my wrong expected text "1 passed, 1 error"; corrected to "2 passed, 1 error")
test_live_state_guard + test_pm_supervisor + test_brief_lint + test_purge_tmp:
417 passed in 29.00s
full suite, no external override:        5944 passed, 12 skipped in 880.84s (0:14:40)   (fullA.log)
full suite, scratch AGENT_WORKFLOW_STATE_DIR: 5944 passed, 12 skipped in 854.87s (0:14:14)   (fullB.log)
both: "live PM state: no metadata change during the run (test-slots excluded)"; the scratch override directory stayed empty.
```
`test_purge_tmp.py` was not edited and passes with the shared fixture (in the 417 above and in both full runs). The 12 skips are the suite's own (platform or tool skips), unchanged.

**Mutants (6, scratch copy of `tools/agent-workflow` only, one edit each, all KILLED):**
1. autouse fixture no longer sets the override: killed (`test_live_state_guard.py`, 1 failed, 7 passed with `-x`);
2. access guard hook is a no-op: killed;
3. `test-slots` exemption dropped: killed;
4. `process_guard` ignores the override: killed (`test_a_reader_follows_the_override...`);
5. teardown no longer fails on a recorded violation: killed (end-to-end child run);
6. `session_context` fixture no longer routes the purge reader: killed (`test_session_context.py`).
Survivors: none. The `test_pm_supervisor.py` and `test_brief_lint.py` fixture fixes are evidenced by their pre-fix failures in `full1.log` rather than a mutant.

**Not run:** ruff lint and `ruff format --check` (ruff absent, installs forbidden); Linux (CI); a run against a main checkout holding a real live ledger (this worktree has none: the guard, which refuses the ledger path even when absent, is the evidence, not a reproduced `git_history_changed`).

**Limits of the guard (as stated in its docstring):** an in-process audit hook covers this process only; children are covered by routing the environment (tested); the metadata snapshot sees writes by any process, never reads, and is reported rather than failed because other sessions may change the live directory.

**Noticed, not done:**
- `workflow_metrics.REGISTRY` ignores `AGENT_WORKFLOW_STATE_DIR` while `cli_worker` writes there (a production inconsistency, not a test leak; left as is).
- A check that a nested pytest or a test child reads the live directory is out of the guard's reach (children).

#### Builder 2 Completion report

- **Behaviour:** every tool test now runs with its own empty state directory; the guard fails a test that touches the live directory beyond `test-slots`; the full suite passes twice with no deselection.
- **Commands and output:** above. **`not_run`:** ruff, Linux, a live ledger in the main checkout.
- **Changed files:** `tests/conftest.py`, `tests/live_state_guard.py` (new), `tests/test_live_state_guard.py` (new), `tests/test_brief_lint.py`, `tests/test_cli_worker_final.py`, `tests/test_pm_supervisor.py`, `tests/test_session_context.py`, `tests/test_setup_project.py`, `tests/test_tool_monitor.py`, this file. No production file, no purge file, no `test_slot.py`, no `pm-supervisor.ps1` changed.
- **Open issues:** the pending decision above; the `workflow_metrics` note.

**Configuration audit (row by row; no new key):**

| Operational value or behaviour | Validated configuration path | Validation | Example/profile | Override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Implicit PM state root of import-time readers (ten attributes) | `AGENT_WORKFLOW_STATE_DIR`, else `<root>/.tmp/pm-state` | The hooks' existing rule, unchanged; empty counts as unset | none (tooling setting) | `test_a_reader_follows_the_override_and_uses_the_default_when_it_is_unset` (two overrides, then unset and empty); refusal: no new refusal, none existed | configurable (unchanged) |
| State root of call-time readers (`cli_worker`, `git_ledger`, `purge_tmp`, `pm_invariants`) | same | unchanged | none | existing tests of each, run in both full runs (override and no override) | configurable (unchanged) |
| Per-test state directory | autouse fixture, `tmp_path_factory` | n/a, test-owned | none | `test_each_test_gets_its_own_empty_state_directory`; child routing test | test-only |
| Live-state guard: protected roots, exempt `test-slots` | `live_roots()`, `SLOTS_NAME` | refuses before the access; recorded | none | `test_a_live_path_is_refused...`, `test_only_the_limiter_subtree...`, `test_the_ambient_override_is_protected_too` (refusal tests), end-to-end child run | safety-bound |
| Audited event table | `PATH_EVENTS` | CPython audit events | none | non-path and unaudited events ignored (`test_a_non_path_argument_is_ignored`) | protocol-bound |
| Limiter location and cap | `test_slot.py`, unchanged | unchanged | none | its own tests, unchanged | artifact-bound (exempt by decision 128) |
| Explicit CLI, profile and configured artifact paths | unchanged | containment checks unchanged | none | the existing tests of those paths pass in both runs | configurable (unchanged) |

#### Builder 2 Worker completion

- [x] Changes: tests, guard and self-tests as listed; no production change.
- [x] Checks: both full runs green, 6 of 6 mutants killed; ruff `not_run`.
- [x] Cleanup: scratch trees, basetemps, mutant copy and scripts removed; kept `full1.log`, `fullA.log`, `fullB.log` under `.tmp/S42-statedir/`. No process left running.
- [x] Remaining: PM review of the pending decision; Linux run in CI.

### Noticed, not done

Step 0 lists `usage_snapshot`, `statusline` and `codex_backend` state paths as explicit configured artifact paths: left as is (approved answer 3).

## Fix round 1 (fresh Claude Sonnet builder; Codex review F1 and F2)

**Step 0 (Small: yes).** Unknowns: how a junction and a symlink change each comparison and walk in the guard. Questions: none (the brief fixes the design: resolve candidates and roots, resolve the exemption the same way, never descend into a link). Plan: (1) class audit; (2) regression tests that fail on the old guard; (3) fix; (4) mutants, focused tests, whole suite; (5) report. Finish: the done check of the brief.

**Class audit (every path comparison and directory walk in the guard, `conftest.py` and the fixtures this task changed):**

| Site | Can a link, junction or alias change the answer? | Result |
|---|---|---|
| `live_state_guard._key` (`abspath` + case) used for the candidate | Yes: `alias/ledger.jsonl` spelled outside the protected tree reaches it (F1) | Fixed: `os.path.realpath` + case; falls back to the absolute spelling when the OS cannot resolve |
| `_KEYS` (roots, resolved once at import, lexical and real both kept) | Yes: a root that becomes a link after import, or a lexical spelling that no longer matches | Fixed: `protected_keys()` resolves the roots on every call; the import-time constant is gone |
| `_EXEMPT` (`test-slots`, lexical and real both kept) | Yes: a lexical `test-slots/redirect` whose target lies outside the subtree was exempt; a `test-slots` that is itself a link made its target exempt (the real key was kept) | Fixed: `exempt_keys()` = `<resolved root>/test-slots`, not resolved again; the candidate is resolved, so a path is exempt only when its target really is the limiter directory |
| `_under` (string prefix) | No on its own: it compares two canonical keys; whole-component rule unchanged | Unchanged |
| `_hook` (`os.fsdecode`, relative `abspath` against cwd) | The candidate now goes through the resolving `_key`; a relative spelling is resolved against cwd first, tested | Covered by the `_key` fix; `dir_fd`-relative and file-descriptor access were never modelled, now said in the docstring |
| `os.symlink` / `os.link` second path in `PATH_EVENTS` | A relative `src` is relative to the link's folder, not cwd: only a missed or extra refusal of the link creation itself; the later access through the new link is caught by resolution | Unchanged, noted |
| `snapshot` descent (`entry.is_dir(follow_symlinks=False)`) | Yes: true for a Windows junction (F2); a cycle never ends | Fixed: no descent when the lstat result has the reparse-point attribute or is a symlink; the link's own metadata stays in the result |
| `snapshot` root `scandir(base)` | The root itself may be a link: intended (it is the watched tree) | Unchanged, documented, tested |
| `snapshot` `entry.name == SLOTS_NAME` | A link cannot change a name comparison; only case on Windows could | Unchanged (outside this class), noted |
| `snapshot_all`, `changes`, `live_roots` | No: dictionary keys and lexical joins of configured roots | Unchanged |
| `conftest.py`: `pm_state_dir` (`mktemp`), `pytest_session*`, `make_link`, `guard_repo` | No path comparison; the snapshot calls are the fixed `snapshot` | Unchanged |
| Changed fixtures (`test_brief_lint`, `test_cli_worker_final`, `test_pm_supervisor`, `test_session_context`, `test_setup_project`, `test_tool_monitor`): `delenv`/`setenv`, a stub, `th.state_dir(CFG, tmp_path)` | No path comparison or walk added | Unchanged |

**Configuration impact / audit:** no runtime edit, no new key. Retained literals unchanged: `SLOTS_NAME` and `STATE_ENV` protocol-bound, `PATH_EVENTS` protocol-bound. Override and refusal tests: no new key; the guard's refusal behaviour is tested below. Property tests of parsers and validators: not applicable (no parser or validator changed).

**New tests** (`test_live_state_guard.py`, 9 cases, scratch only; links via `conftest.make_link`, skipped when the platform refuses a link; every link sits inside a pytest `tmp_path` and goes with its basetemp):
- refused and recorded: a junction alias into a synthetic protected tree (`open` and `listdir`), a chain of aliases, a relative alias after `chdir`, a protected root that is itself a link (both spellings);
- exemption: a link inside `test-slots` that leaves the subtree is not exempt (into protected state), one that leaves the protected tree is unprotected; a `test-slots` that is itself a link exempts nothing it points at;
- an unresolvable path falls back to its absolute spelling; a link cycle gives an answer;
- snapshot: a junction to an outside directory and a cycle back to the root are listed, not descended; a change behind a link is not reported; a link as the root is walked.

**Old guard versus fixed guard (real output).** On the unfixed tree, the first 8 of these (`-k "link or cycle or chain or recorded"`): `5 failed, 3 passed`; the passing ones were controls, among them the cycle test, which only guards the robustness of the fix. Caveat: the tests that patch `_ROOTS` failed on the old tree partly because the old import-time keys ignored the patch; the lexical-key mutant M1 below shows each of them fails for the alias itself. After the fix: `27 passed in 4.50s` for the file at that point; focused set (`test_live_state_guard.py`, `test_cli_worker_codex.py`, `test_codex_hooks.py`, `test_purge_tmp.py`): `718 passed, 1 skipped in 154.96s (0:02:34)`.

**Mutants (6, scratch copy of the guard, the self-tests and a minimal conftest; the 2 control failures are the `precompact_snapshot` reader tests, which need the real tree and are an artefact of the copy):**

| Mutant | Killed by |
|---|---|
| M1 `_key` lexical (`abspath` instead of `realpath`) | alias, chain, relative alias, root link, limiter-subtree link, link-as-`test-slots` tests (5 more failures than control) |
| M2 junction check off in `snapshot` | `test_snapshot_keeps_a_link_s_own_metadata_and_never_descends_into_it` |
| M3 exemption resolves `test-slots` itself | `test_a_limiter_directory_that_is_itself_a_link_exempts_nothing_it_points_at` |
| M4 unresolvable-path fallback removed | `test_an_unresolvable_path_falls_back_to_its_absolute_spelling` |
| M5 roots compared lexically | `test_a_protected_root_that_is_itself_a_link_is_refused_by_either_spelling` |
| M6 whole-component prefix rule dropped | `test_only_the_limiter_subtree_of_the_live_directory_is_exempt` |

Survivors: none. Not mutated: the POSIX `S_ISLNK` half of `_is_reparse_point` (unreachable on Windows; the Linux symlink path runs in CI).

**Whole tool suite, once, through the limiter, fixed `--basetemp` (deleted after the run):** `5953 passed, 12 skipped in 919.56s (0:15:19)`; `live PM state: no metadata change during the run (test-slots excluded)` (`.tmp/S42-statedir/full-fix1.log`). Earlier round: 5944 passed; the 9 new tests account for the difference.

**Lint:** `ruff` is not installed in the project `.venv` and installs are forbidden: lint and `ruff format --check` are `not_run`. Linux execution `not_run` (CI).

**Cost note:** every audited path event now calls `realpath` once for the candidate and once per root; the whole suite took 919 s against 855 to 881 s before (single runs, machine load not controlled).

**Changed files (this round):** `tools/agent-workflow/tests/live_state_guard.py`, `tools/agent-workflow/tests/test_live_state_guard.py`, this task file. No production file, `purge_tmp.py` or `test_purge_tmp.py` edited.

**Noticed, not done:** the `snapshot` exclusion of `test-slots` compares names with case (Windows could differ from the exemption's case-normalized rule); `dir_fd`-relative and native I/O stay outside the guard (stated in the docstring).

### Fix round 1 Worker completion

- [x] Changes: guard resolves candidate, roots and exemption; snapshot never descends into a link or junction; docstring claims only what holds; 9 regression tests.
- [x] Checks: focused 718 passed; whole suite 5953 passed, 12 skipped; 6 of 6 mutants killed; ruff and Linux `not_run`.
- [x] Cleanup: scratch basetemps, mutant copy and scripts removed; `full-fix1.log` kept under `.tmp/S42-statedir/`. No process left running, no link left.
- [x] Remaining: PM re-review of F1/F2; Linux run in CI.

## Fix round 2 (fresh Claude Sonnet builder; Codex retest findings R1F1 to R1F4)

**Step 0 (Small: yes).** Unknowns: how each spelling and a resolver error reach the comparison key. Questions: none (the PM decisions fix the design: one key, fail closed, narrowed scope). Plan: (1) key audit; (2) tests; (3) fix; (4) probes, mutants, focused tests, whole suite; (5) report. Finish: the brief's done check.

**Key audit: every place the guard turns a path into a key or compares a name** (`\\?\` = extended prefix, `\\.\` = device prefix; the first row of each pair is round 1, the second round 2):

| Site | `\\?\` and `\\?\UNC\` | `\\.\` | Case | Resolver error |
|---|---|---|---|---|
| Candidate (`_hook` to `is_protected` to `_key`) | R1: kept by `ntpath.realpath` (experiment: extended spelling ALLOWED). R2: prefix removed in `_key`, `\\?\UNC\s\h` becomes `\\s\h` | `realpath` already returns the ordinary spelling (experiment: `\\.\C:\x` gives `C:\x`); also stripped in `_plain_spelling`; a volume-GUID spelling resolves to `\\?\D:\...` and is then stripped (experiment) | `normcase` | R1: lexical fallback (fail open). R2: `Unresolvable`, hook refuses and records |
| Protected roots (`protected_keys`) | same `_key` | same | same | R2: `Unresolvable`: every access is refused (a root with no verdict gives no safe answer) |
| Exemption (`exempt_keys`) | same `_key` of the first root, then the case-normalized `test-slots` name | same | same | R2: `Unresolvable` |
| `_under` | text prefix over two keys of the same form | n/a | n/a | n/a |
| `snapshot` limiter filter | no path text, only the entry name | n/a | R1: literal `==`. R2: `normcase` on both sides (POSIX stays case-sensitive) | n/a |
| `snapshot` descent, `changes`, `snapshot_all`, `live_roots` | not keys; relative names of one walk | n/a | n/a | `scandir`/`stat` errors skip the entry, as before (a metadata report, not an enforcement) |

Residual spelling: a loopback share of a local drive (`\\?\UNC\localhost\D$\...`) stays a distinct key (experiment: `realpath` keeps it); stated in the docstring, outside the accidental-access scope.

**Changes (one shared `_key`):** `_plain_spelling` (pure text) and `_key` (resolve, strip on Windows, `normcase`; raise `Unresolvable` on `OSError`/`ValueError`); the hook records `... (cannot be classified: ...)` and raises `LiveStateAccess`; `snapshot` compares `normcase` names; the real I/O path is untouched. The docstring now states the scope (PM decision, pending user review): protection against accidental access in a stable layout, not against a concurrent actor. R1F3 (a link created or swapped during a check or a scan) is accepted scope, not fixed, and no test pretends otherwise.

**New tests (`test_live_state_guard.py`):** candidate the resolver refuses (OSError and ValueError, with a real junction alias and a labelled resolver double) is refused and recorded; a refused root refuses every access and `protected_keys`/`exempt_keys` raise; a missing tail is a normal answer; the Windows prefixes reduce structurally (pure text, no network, every platform); an extended-length candidate against an ordinary root (also a real `open`), an extended-length root against an ordinary candidate (Windows only); case variants of a candidate and of the limiter (Windows only); snapshot skips `TEST-SLOTS` on Windows and keeps it on a case-sensitive system (the other skips on this platform).

**Round 1 code versus fixed code (scratch re-creation of round 1: lexical fallback, no prefix removal, literal snapshot name):** 6 of the new tests fail on it (`candidate_the_resolver_refuses` x2, `root_the_resolver_refuses`, `extended_length_candidate`, `extended_length_root`, `snapshot_skips_the_limiter_directory_in_any_case`); the other new tests pass there and guard behaviour kept (missing tail, case, structural prefix text, link cases). All pass on the fixed tree.

**Retained retest probes, read in place, on this tree:**
```text
run_spellings.py: ordinary BLOCKED violations 1 / extended BLOCKED violations 1 / device BLOCKED violations 1 / extended_root_ordinary_candidate_protected True
run.py edges:     case_variant_snapshot []   fallback_alias_resolver_double BLOCKED violations 1
                  link_created_after_roots_resolve ALLOWED 'synthetic ledger' violations 0   (R1F3, accepted scope)
run_snapshot_race.py: snapshot_followed_new_junction True   (R1F3, accepted scope)
```
The probes' scratch trees were removed with the reviewer's own `cleanup.py` (links removed as links; it also rewrote `retest1/cleanup.log`).

**Mutants (7 changed-line variants plus the round 1 re-creation; scratch copy of guard, self-tests, minimal conftest; the 2 control failures are the `precompact_snapshot` reader tests, a copy artefact):** prefix removal off, UNC conversion dropped, lexical fallback restored, snapshot name literal, `normcase` dropped from the key (first survived: `realpath` already restores the true case of existing parts; a Windows test with a missing `test-slots` tail now kills it), hook lets `Unresolvable` escape unrecorded, exemption keyed lexically: 7 of 7 killed, none survive. Not mutated: the POSIX `S_ISLNK` half (Linux, CI).

**Tests:** focused (`test_live_state_guard.py`, `test_cli_worker_codex.py`, `test_codex_hooks.py`, `test_purge_tmp.py`): `726 passed, 2 skipped in 160.45s (0:02:40)`. Whole tool suite, once, through the limiter, fixed `--basetemp` (deleted): `5961 passed, 13 skipped in 940.10s (0:15:40)`; `live PM state: no metadata change during the run (test-slots excluded)` (`.tmp/S42-statedir/full-fix2.log`). The extra skip is the POSIX-only snapshot test on Windows.

**Configuration audit:** no runtime edit, no new key; `SLOTS_NAME`, `STATE_ENV`, the Windows prefixes and `PATH_EVENTS` are protocol-bound literals; no parser or validator changed (property tests not applicable). **`not_run`:** ruff lint and format check (ruff absent, installs forbidden); Linux; a real symlink (no privilege on this machine, junctions used); a network share.

**Changed files (this round):** `tools/agent-workflow/tests/live_state_guard.py`, `tools/agent-workflow/tests/test_live_state_guard.py`, this task file. No production file, `purge_tmp.py` or `test_purge_tmp.py`.

**Noticed, not done:** the round 1 audit table above says "falls back to the absolute spelling"; round 2 replaces that with a refusal.

### Fix round 2 Worker completion

- [x] Changes: one comparison key with prefix removal and case; fail closed on a resolver error; snapshot limiter filter by `normcase`; docstring scope narrowed (R1F3 accepted).
- [x] Checks: 6 new tests fail on the round 1 behaviour; focused 726 passed, 2 skipped; whole suite 5961 passed, 13 skipped; 7 of 7 mutants killed; probes as expected; ruff and Linux `not_run`.
- [x] Cleanup: basetemps, mutant copy, scripts and probe trees removed; `full-fix2.log` kept. No process left, no link left.
- [x] Remaining: PM re-review; Linux run in CI.

## Fix round 3 (fresh Claude Sonnet builder; Codex retest 2, R2F1)

**Step 0 (Small: yes).** Unknown: which spellings Win32 normalization equates that `_key` keeps apart. Questions: none (PM decision: `abspath` before `realpath` on Windows, `\\?\` verbatim). Plan: (1) cite the sources; (2) tests; (3) fix; (4) probe, mutants, focused tests, whole suite; (5) report. Finish: the brief's done check.

**Research evidence (primary sources reread):**
- Installed `ntpath.py` (Python 3.12.10): on Windows `abspath` is `return _getfullpathname(normpath(path))` (lines 581-584; `_getfullpathname` is imported from `nt` at line 566), which wraps `GetFullPathNameW`. `realpath` (line 705) starts `path = normpath(path)`, then calls `_getfinalpathname`; on `OSError` it uses `_getfinalpathname_nonstrict`, which keeps the missing tail (round 2 retest, `installed-ntpath.txt`): no trailing-period or space trimming.
- Microsoft, "File path formats on Windows systems" (https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats), read once for these quotes, a public documentation page, no project data sent: "Almost all paths passed to Windows APIs are normalized. During normalization, Windows performs the following steps: ... Canonicalizes component and directory separators. Evaluates relative directory components ... Trims certain characters." and "This normalization happens implicitly, but you can do it explicitly by calling the Path.GetFullPath method, which wraps a call to the GetFullPathName() function."; trimming: "If the path doesn't end in a separator, all trailing periods and spaces (U+0020) are removed."; skipping: "Normally, any path passed to a Windows API is (effectively) passed to the GetFullPathName function and normalized. There is one important exception: a device path that begins with a question mark instead of a period. Unless the path starts exactly with `\\?\` (note the use of the canonical backslash), it is normalized." Source-verified (documentation).
- Experiment (round 2 retest, reproduced by my test): `os.mkdir('<absent root>.')` and `os.mkdir('<absent root> ')` created `pm-state` while the round 2 key kept the dot or space.

**Class audit (spellings Win32 treats as equal):** trailing periods and spaces (the finding), forward and mixed separators, doubled separators, `.` and `..` segments, the current directory for relative paths, case. After the fix all of them pass through the one `GetFullPathNameW` step (or `normcase`); for Windows directory operations a trailing separator keeps a trailing-space name distinct, as the documentation says, and the tests exclude it. The guard's key does not keep it distinct: `abspath` and `realpath` both start with the lexical `normpath` (installed `ntpath.py:584`), which drops the trailing separator, so a distinct extended-namespace `pm-state ` shares the key of `pm-state` and is refused. This is a conservative false refusal, already present in the round 2 key (retest 3 R3F1, `prior_same_key True`), accepted by the PM. `\\?\` paths skip only the new `abspath` step; `realpath` normalizes and resolves them as before, then the prefix is removed. Not covered, as before: a loopback share spelling of a local drive.

**Change (one site, `_key`):** `text = os.fsdecode(path)`; on Windows and when `text` does not start exactly with `\\?\`, `text = os.path.abspath(text)`; then `realpath`, prefix removal, `normcase`, `Unresolvable` as before. POSIX untouched. Docstring of `_key` states the reason.

**New tests (`test_live_state_guard.py`, Windows only except where noted):**
- property tests (Hypothesis, `ci` profile, 150 examples each): every spelling of one absent root has one key (mixed separators, case variants, `.`/`..`/doubled-separator segments before the last component, trailing `.`, space, `. `, ` .`, two spaces, `..`, `...`); the same for one existing root, plus its `\\?\` spelling; a child of the absent root keeps the key under spellings that trim a single period;
- real audited `os.mkdir` of the absent root through `.`, space, `. ` and `...` (also with `/` separators) is refused, recorded, and creates nothing (4 cases).

**New tests versus round 2 code (scratch re-creation: the `abspath` step removed):** the absent-root property and the 4 `mkdir` cases fail (5 failed); the existing-root and child properties pass there and guard behaviour kept.

**Retained probe `retest2/missing_and_hooks.py`, run in place on this tree:**
```text
missing_root '.' ... protected True
missing_root '.' mkdir BLOCKED violations 1 real_root_created False
missing_root ' ' ... protected True
missing_root ' ' mkdir BLOCKED violations 1 real_root_created False
fail_closed_event_position_stage_checks 69 all_refused_and_recorded
```
(its scratch `missing-tree` removed; no link was created).

**Mutants (4 plus the round 2 re-creation; scratch copy of guard, self-tests, minimal conftest; control `16 passed`):** `abspath` only for the extended spelling, `normpath` instead of `abspath`, Windows branch off, `normcase` dropped: 4 of 4 killed, none survive (the first three by the absent-root property and the `mkdir` tests, the last by the absent-root property and the case test).

**Tests:** focused (`test_live_state_guard.py`, `test_cli_worker_codex.py`, `test_codex_hooks.py`, `test_purge_tmp.py`): `733 passed, 2 skipped in 164.46s (0:02:44)`. Whole tool suite once through the limiter, fixed `--basetemp` (deleted): `5968 passed, 13 skipped in 940.64s (0:15:40)`; `live PM state: no metadata change during the run (test-slots excluded)` (`.tmp/S42-statedir/full-fix3.log`).

**Docstring scope (PM decision, unchanged):** accidental access in a stable layout; a link created or swapped during a check is not covered (R1F3/R2N1, accepted).

**Configuration audit:** no runtime edit, no new key; no new literal (the prefix text `\\?\` already existed, protocol-bound). **`not_run`:** ruff (absent), Linux (CI), a real symlink (no privilege), a genuine 8.3 short name and an NTFS stream spelling (not exercised; `abspath` normalization applies before them but no test covers them).

**Changed files (this round):** `tools/agent-workflow/tests/live_state_guard.py`, `tools/agent-workflow/tests/test_live_state_guard.py`, this task file. No production file, `purge_tmp.py` or `test_purge_tmp.py`. One network read: the Microsoft documentation page above (public, no project information sent); no other network use.

### Fix round 3 Worker completion

- [x] Changes: one Win32 normalization step in `_key`; property and real `mkdir` tests; no other file.
- [x] Checks: 5 new failures on the round 2 behaviour; focused 733 passed, 2 skipped; whole suite 5968 passed, 13 skipped; 4 of 4 mutants killed; probe lines now BLOCKED; ruff and Linux `not_run`.
- [x] Cleanup: basetemp, mutant copy, scripts and the probe tree removed; `full-fix3.log` kept. No process left, no link created.
- [x] Remaining: PM re-review; Linux run in CI.
