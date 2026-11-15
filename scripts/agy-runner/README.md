# Antigravity (`agy`) restricted runner (Windows only)

Lets Gemini through `agy` edit code and run tests in a copy of the project without reaching `.secrets/`, the operator's profile or other projects. Policy: [PM_POLICY](../../.agents/policy/PM_POLICY.md) section 3. Model facts: [N095](../../docs/note/N095-gemini-agy-models.md).

## How it works

- `setup.ps1` (run once by the machine owner, elevated) creates the local account `agy-runner`, grants it "Log on as a batch job", denies it the folders in `-DenyAllRoots`, re-allows the project read-only, denies `.secrets` again, allows writes only to `.tmp/agy-work`, copies `agy.exe` and `agy-job.ps1` into an admin-only tool folder and registers this project's scheduled task. The password is typed, never printed or stored by the script. Then sign the account in once with the `runas` line it prints.
- **One task per project, one account for all.** The task name is `AgyRunner-<leaf>-<hash8>`. `<leaf>` is the project folder name with every character outside `A-Z a-z 0-9 . _ -` turned into `-`, lower-cased and cut to 40. `<hash8>` is the first 8 hex digits of SHA-256 over the upper-cased canonical root: every link and junction on the path is resolved, and each existing name takes its on-disk long form. `Get-AgyRunnerTaskName` in `agy-lib.ps1` is the only implementation; `setup_project.py check` asks it through one read-only PowerShell call. `agy-lib.ps1` is also the single source of the action classification (`Get-AgyActionClass`, `Get-AgyTaskClass`), which every script and the check use. A task binds a project only in setup's layout: one action, program `powershell.exe`, the arguments `[-NoProfile] [-ExecutionPolicy Bypass] -File <fully qualified ...\agy-job.ps1> -ProjectRoot <fully qualified path>` with nothing after them, each host option and its value a separate argument. Any other action that names `agy-job.ps1` (a bare or relative wrapper path, a combined `"-ExecutionPolicy Bypass"` argument, a `-Command` script) binds nothing, but it still counts as a runner task for the idle check and for `undo.ps1`'s decision to keep the shared parts. It also owns the path comparison and the rule decision. The account `agy-runner`, its sign-in and the tool folder are shared. A task bound to another folder is never re-registered, changed or removed: `setup.ps1` stops, and `undo.ps1` reports it and keeps the shared parts while any runner task remains. The older single task name `AgyRunner` belongs to another project: it is never derived, and every script refuses it as an explicit `-TaskName`. Every scheduler, folder and ACL read in these scripts either succeeds or stops with its error, so a failed read never looks like "no task" or "no folder"; `undo.ps1` runs all its checks before its first removal. The runner takes one job at a time across all projects: `Assert-AgyIdle` refuses while any runner task is running.
- The operator (or the PM's tools) writes a job into `.tmp/agy-jobs/job.json` plus a prompt file; `agy-job.ps1` validates every field (ids, workspace, model, timeout, conversation), runs `agy -p` inside `.tmp/agy-work/<workspace>` with temp files kept in the workspace, and writes `_results/<job>.json`, `.stderr.txt`, `.error.txt` and `.done`.
- `invoke-job.ps1` submits one job and waits; one job at a time. `tools/agent-workflow/gemini-launch.ps1`, `gemini-ws.ps1` (workspace from a frozen commit, `-Sanitize`) and `gemini-review.ps1` (read-only jobs under the operator's account) build on `agy-lib.ps1`.
- `update.ps1` updates `agy` and refreshes the protected copy; `undo.ps1` removes everything `setup.ps1` made (same parameters).

## Set up the runner for this project

Only the machine owner runs these steps, in an elevated Windows PowerShell; agents never run them.

1. Read-only first: `python -B tools/agent-workflow/setup_project.py check --no-cli` shows the `runner` rows: this project's task, which root each runner task serves, the job folder and the rules on `.tmp\agy-work`.
2. Run `scripts\agy-runner\setup.ps1 -DenyAllRoots <folders>` from this checkout (the main tree, not a worktree; the task name comes from the root). Before any change it stops when a task of the derived name serves another folder, or when a folder it must grant carries an explicit Deny rule for the account; the message names the path and the rule. It never removes a rule.
3. Sign the account in with the printed `runas` line if it has never signed in.
4. Run step 1 again: the task, `.tmp\agy-jobs`, `.tmp\agy-work` and Allow-rule rows are each `OK`; each missing item has its own `WARN`. `gemini-launch.ps1` refuses until then (task missing, bound to another root, or job folder missing), and read-only reviews keep working through `gemini-review.ps1`.

**State seen on 2026-11-15 (open question for the user).** On this machine the account has an explicit (not inherited) `Deny FullControl (CI)(OI)` on the folder that holds the projects (the parent of `<project>`), on this project root, on `.tmp\agy-work` and on folders created later inside the project (read with `Get-Acl`; `.secrets` was not read), so `setup.ps1` stops at the project root. The runner needs an explicit `Allow ReadAndExecute (CI)(OI)` on the project root and `Allow Modify (CI)(OI)` on `.tmp\agy-work`; `.secrets` must stay denied to it. Why these Deny rules are explicit is not known yet (`.tmp/agy-rca/report.md`, Q2). How to clear them on this project without touching `.secrets` is the user's decision; no recursive removal is given here, because it would also lift the `.secrets` Deny.

## Failure classes the helpers guard against (`agy-lib.ps1`)

| ID | Failure | Guard |
| --- | --- | --- |
| F1 | A read outside `.tmp/agy-work` is auto-denied and silently ends the turn | path lint of the prompt and `notes/*.md` (`Assert-AgyPathLint`); `agy_sanitize.py` rewrites paths first |
| F2 | Extra notes copied from protected or linked locations | `Resolve-AgyExtraNote`: Markdown only, inside the root, no links, no `.secrets`, `models`, `.venv`, `.git` segment |
| F3 | The wrapper reports exit 0 and `SUCCESS` after a denied action, an empty reply or a background wait | `Get-AgyIncompleteReason`: status `INCOMPLETE`, exit 2 |
| F4 | Review jobs start background commands or end a turn while waiting | fixed review suffix (`Add-AgyReviewSuffix`) |
| F5 | A stale or misplaced expected file passes as a result | `-ExpectFile` must be workspace-relative and newer than the job |
| F6 | A second job submitted while one runs is ignored by the task | `Assert-AgyIdle` refuses and names the running job, also another project's runner task |
| F7 | The task serves another project, or is missing, so a job is never picked up | `Assert-AgyRunnerBinding` refuses before any side effect; `setup_project.py check` shows the binding |

Quota: `Get-AgyQuotaNotice` reads a `RESOURCE_EXHAUSTED` error and infers the 5-hour or weekly window from the stated wait.

## Model

`invoke-job.ps1 -Model` defaults to `gemini-3.8-flash-high`, the identifier the runner used in practice. Google's documentation names the model "Gemini 3.8 Flash" with a separate effort setting and does not list this string (N095): confirm with `agy models` after each `agy` update and change the default here if it no longer resolves.
