# Antigravity (`agy`) restricted runner (Windows only)

Lets Gemini through `agy` edit code and run tests in a copy of the project without reaching `.secrets/`, the operator's profile or other projects. Policy: [PM_POLICY](../../.agents/policy/PM_POLICY.md) section 3. Model facts: [N095](../../docs/note/N095-gemini-agy-models.md).

## How it works

- `setup.ps1` (run once by the machine owner, elevated) creates the local account `agy-runner`, grants it "Log on as a batch job", denies it the folders in `-DenyAllRoots`, re-allows the project read-only, denies `.secrets` again, allows writes only to `.tmp/agy-work`, copies `agy.exe` and `agy-job.ps1` into an admin-only tool folder and registers the `AgyRunner` scheduled task. The password is typed, never printed or stored by the script. Then sign the account in once with the `runas` line it prints.
- The operator (or the PM's tools) writes a job into `.tmp/agy-jobs/job.json` plus a prompt file; `agy-job.ps1` validates every field (ids, workspace, model, timeout, conversation), runs `agy -p` inside `.tmp/agy-work/<workspace>` with temp files kept in the workspace, and writes `_results/<job>.json`, `.stderr.txt`, `.error.txt` and `.done`.
- `invoke-job.ps1` submits one job and waits; one job at a time. `tools/agent-workflow/gemini-launch.ps1`, `gemini-ws.ps1` (workspace from a frozen commit, `-Sanitize`) and `gemini-review.ps1` (read-only jobs under the operator's account) build on `agy-lib.ps1`.
- `update.ps1` updates `agy` and refreshes the protected copy; `undo.ps1` removes everything `setup.ps1` made (same parameters).

## Failure classes the helpers guard against (`agy-lib.ps1`)

| ID | Failure | Guard |
| --- | --- | --- |
| F1 | A read outside `.tmp/agy-work` is auto-denied and silently ends the turn | path lint of the prompt and `notes/*.md` (`Assert-AgyPathLint`); `agy_sanitize.py` rewrites paths first |
| F2 | Extra notes copied from protected or linked locations | `Resolve-AgyExtraNote`: Markdown only, inside the root, no links, no `.secrets`, `models`, `.venv`, `.git` segment |
| F3 | The wrapper reports exit 0 and `SUCCESS` after a denied action, an empty reply or a background wait | `Get-AgyIncompleteReason`: status `INCOMPLETE`, exit 2 |
| F4 | Review jobs start background commands or end a turn while waiting | fixed review suffix (`Add-AgyReviewSuffix`) |
| F5 | A stale or misplaced expected file passes as a result | `-ExpectFile` must be workspace-relative and newer than the job |
| F6 | A second job submitted while one runs is ignored by the task | `Assert-AgyIdle` refuses and names the running job |

Quota: `Get-AgyQuotaNotice` reads a `RESOURCE_EXHAUSTED` error and infers the 5-hour or weekly window from the stated wait.

## Model

`invoke-job.ps1 -Model` defaults to `gemini-3.8-flash-high`, the identifier the runner used in practice. Google's documentation names the model "Gemini 3.8 Flash" with a separate effort setting and does not list this string (N095): confirm with `agy models` after each `agy` update and change the default here if it no longer resolves.
