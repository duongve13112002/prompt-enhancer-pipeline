# Continuous integration

How to run CI (T-022, R-025, decisions 26, 77, 111, 112). GitHub behaviour below is quoted from the primary pages in [N134](note/N134-github-visibility-and-actions.md); a claim marked *unverified* was not re-read there.

**Repository and credentials (decision 35).** The user creates the GitHub repository, clones it and sets up `gh` before agents start; agents work inside that clone and never create a repository, choose a remote or log in themselves.

## Requirement

Continuous integration runs on GitHub Actions with the latest Ubuntu and Windows runners. For a run the repository is made public, and it is made private again when the run ends, whether green or red (R-025, decision 26).

## Workflow

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

- **Trigger:** `workflow_dispatch` only, so a push never starts a run while the repository is private. GitHub runs it only "if the workflow file exists on the default branch" (N134).
- **Matrix (decision 168):** `ubuntu-latest` and `windows-latest`, times the suites `pipeline` and `tooling`, each split into shards: nine parallel jobs, with `fail-fast: false`. One `include` entry per job (`os`, `suite`, `shard`, `shards`):

  | Leg | Shards | Why |
  | --- | --- | --- |
  | Windows pipeline | 4 | CI window 25 (run 44415756388): 51 min |
  | Ubuntu pipeline | 2 | 20 min |
  | Windows tooling | 2 | 19 min |
  | Ubuntu tooling | 1 | 6 min (already below the target) |

  - Each job installs the CPU profile (`pip install ".[test]"`: the pyproject dependencies and the test extra, no GPU package).
  - It then runs its suite exactly as before: the pipeline suite (`tests/`, `PYTHONPATH=src`) or the tooling suite (`tools/agent-workflow/tests`). The shard comes in through the environment, `PE_SHARD` (1-based) and `PE_SHARDS` (count), set from the matrix; the pytest command line does not change.
  - **Why one job per suite:** in run 43820753462 the Windows job ran both suites in turn. The pipeline suite alone took 23 min 27 s, so the 30 min limit cancelled the tooling suite.
  - **Job limit: 60 min,** unchanged. This is a PM value pending user review, kept from before the sharding because no sharded run has been measured yet. After window 26 it can be lowered together with `run_timeout_seconds`.
  - Nothing is skipped, deselected by name or allowed to fail. There is no GPU, no model download, no secret and no paid call.
- **Sharding** ([`ci_shard.py`](../tools/agent-workflow/ci_shard.py), weights in [`ci_shard_weights.json`](../tools/agent-workflow/ci_shard_weights.json)):
  - **Selector:** a pytest collection hook, loaded from each suite's `conftest.py`. It assigns whole test files to shards, longest first by weight to the least-loaded shard, and deselects the tests of the other shards' files (pytest reports the deselected count). Every shard computes the same partition on its own from the set of collected files and the table, so no coordination is needed. Without the variables the hook does nothing and the full suite runs.
  - **Why a hook and not a file-list script:** the partition is made over the files pytest actually collected, so "every test exactly once" follows from "every file in exactly one shard" with no second file discovery to disagree with pytest, and a shard command line needs no shell-specific quoting on Windows and Linux.
  - **Weights:** seconds of test time per file, measured once on the dev machine, one pytest process per file (sum of the junit testcase times), 133 pipeline files (2995 s, 7792 tests) and 52 tooling files (1538 s, 6461 tests). A file missing from the table gets `default_weight_s` (the median) and is still assigned; the shard line printed at collection shows how many files used it. Refresh the table after large changes to the suites; only the balance depends on it, never which tests run.
  - **Forecast of the slowest job** (weight share of the heaviest shard, scaled to the CI window 25 time of the leg; the job time adds checkout, Python setup and `pip install`, which were not measured separately and are assumed to be 2 to 3 min): Windows pipeline, 4 shards, 25 % of 51 min is about 12.8 min; Ubuntu pipeline, 2 shards, 50 % of 20 min is about 10 min (Windows weights, an extrapolation: POSIX-only tests are skipped on Windows, so per-file ratios on Linux can differ); Windows tooling, 2 shards, 50 % of 19 min is about 9.5 min; Ubuntu tooling, 1 shard, about 6 min (the whole suite, no split). The slowest job is therefore expected near 15 min, against 51 min before. This is a forecast from local weights, not a measurement; window 26 gives the first real figures.
  - **Refusals:** a malformed, zero, negative, out-of-range or half-set value fails the job in the step "Refuse a malformed shard" (`ci_shard.py check`, which also needs both variables set) and again in the hook; a shard that selects no test fails instead of passing green. The two variables are CI protocol values, not profile settings.
  - **Proof:** [`test_ci_shard.py`](../tools/agent-workflow/tests/test_ci_shard.py) lives in the tooling suite, so it runs in the tooling jobs only (Windows shard that holds it, and Ubuntu tooling), not in the pipeline jobs. Every job uses the same selector code, so the proof carries over. The test checks, over the real `tests/` and `tools/agent-workflow/tests/` collections (collected once by a child pytest, 1 to 8 shards), that the shards are disjoint and join to the whole, with a Hypothesis property test over file lists, weights and shard counts and child-pytest runs of the hook over synthetic projects, including one session over two directories whose conftests both register the hook (applied once). It also checks that every shard of every suite is in the matrix. At run time no job compares its own count with the other shards'; the per-shard counts of the real suites were summed by hand (decision 168 task file).
  - **Child processes:** the shard variables are process environment, so a test that starts a child pytest loading a conftest must take its environment from `ci_shard.child_environ()` (a guard test scans both suites for the spawn pattern).
  - **Exit stress:** the Linux opt-in check runs once, in Ubuntu pipeline shard 1, as a separate step that names one test and carries no shard variable.
- **Token:** `permissions: contents: read`.
- **Action pins:** each action is pinned to a full commit SHA, with its tag in a comment.
  - `actions/checkout` v7.0.1 is `3d3c42e5aac5ba805825da76410c181273ba90b1`.
  - `actions/setup-python` v7.1.0 is `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`.
  - **Source:** the PM read them from the official repositories with `gh api repos/actions/<name>/releases/latest` and `gh api repos/actions/<name>/git/ref/tags/<tag>` on 2026-11-15 at 16:12. Both tags point straight at a commit.
  - `ci_window.py` refuses to open while any `uses:` is not a 40-hex SHA.
  - **The inputs used still hold in v7.** Checked in each README at the pinned commit (`raw.githubusercontent.com/actions/<name>/<sha>/README.md`):
    - checkout: "Set `persist-credentials: false` to opt-out."
    - setup-python: "No changes to action inputs, outputs, or behavior." (What's new in V7)
    - setup-python still documents `python-version: '3.13'` in its usage.
- **Known reds:** the 10 tooling tests that failed in a Linux container (`tasks/STATUS.md`, "Windows-specific behaviour") are expected to fail on the Ubuntu leg. The first window names them, and the PM opens a fix task. They are never skipped.

## Running a window

The orchestrator only, after each accepted and pushed task, or when the user asks (decision 112):

```
<venv python> -B tools/agent-workflow/ci_window.py run --repo <main working tree>
```

Settings: [`tools/agent-workflow/ci_window_config.json`](../tools/agent-workflow/ci_window_config.json). Every key is required.

- **Reused from [`guard_config.json`](../tools/agent-workflow/guard_config.json):** the remote and branch (`git_guard.push_remote`, `push_branch`), the forbidden paths (`push_guard.forbidden_*`) and `git_timeout_seconds`.
- **`run_timeout_seconds` = 4200** was refit after run 43820753462. It is above the 60 min job limit, with a margin for queueing. It is a PM value pending user review. Decision 168 leaves it unchanged: the sharded jobs are expected to take about 15 min, but only a measured window shows the real figure.

Steps, in the order the tool runs them:

1. **Preconditions.** Each fails closed with exit 2, and nothing is changed:
   - the working tree is clean, untracked files included;
   - HEAD is `refs/heads/<branch>` and equals that branch on the remote (`git ls-remote`, which writes nothing);
   - every commit any remote ref names is local;
   - no path in the history of any remote ref is under a forbidden prefix (`push_guard`'s, plus `runs`) or has a forbidden suffix. The exact paths in `allowed_tracked_paths` (`.secrets/README.md`, `models/README.md`) are allowed;
   - every workflow `uses:` is pinned;
   - the secret scanner passes over the same history.

   The whole history is checked because going public shows all of it: "The code will be visible to everyone who can visit GitHub.com." (N134).
2. **Record** the visibility (`gh repo view --json visibility`).
3. **Switch to public** (`gh repo edit --visibility public --accept-visibility-change-consequences`), only when the repository was private.
   - Just before the switch, while still private, the tool reads `git ls-remote` and the local HEAD again. If any ref was added, moved or removed since the scan, or HEAD moved, it refuses with exit 2 and changes nothing (review fix round 1, F2).
   - **Then the non-Git surfaces** (fix round 2, RT1; a PM decision pending user review), read with `gh api` (GET only) while still private. Any of these refuses with exit 2:
     - `has_wiki`, `has_discussions` or `has_pages` is true (`GET repos/{owner}/{repo}`). The tool never changes these settings. "When you turn off a wiki, the content is hidden, but not erased." ([disabling wikis](https://docs.github.com/en/communities/documenting-your-project-with-wikis/disabling-wikis)).
     - Any release exists, drafts included (`GET …/releases?per_page=1`). "Information about published releases are available to everyone." ([releases](https://docs.github.com/en/rest/releases/releases#list-releases)).
     - Any issue or pull request exists, in any state (`GET …/issues?state=all&per_page=1`). "GitHub's REST API considers every pull request an issue" ([issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)).
     - A retained workflow run (`GET …/actions/runs`) or artifact (`GET …/actions/artifacts`) does not belong to a run in `run_ledger` (`runs/ci-window/ledger.json`), or an artifact names no run. Both lists are read page by page (`per_page=100`) with no filter. The 1,000-result cap applies only "when using the following parameters: actor, branch, check_suite_id, created, event, head_sha, status" ([workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)). Every page must report the same `total_count`, no id may repeat, and the items read must equal that count.
     - Any `gh` error, missing permission, timeout, non-JSON body, missing field or wrong type.
   - The window records each dispatched run id in `run_ledger` as soon as it knows it. A failed write is a window error: the tool returns to private and exits 1, and the next window refuses on the unrecorded run until the ledger is repaired.
   - **The ledger is trusted local state** (fix round 3). Every read and every write holds `<run_ledger>.lock` through the project's OS file lock (`hooks/git_ledger.py`), so two windows never interleave a read with a write; a lock still busy after `ledger_lock_timeout_seconds` refuses the read (exit 2) or fails the write (exit 1, private). A write goes to a temp file of its own and one `os.replace`. The schema version and every run id must be JSON integers: `true` and `1.0` refuse.
   - **No link below the project root, checked on the opened object** (fix round 4, `tools/agent-workflow/trusted_state.py`). The ledger, its lock, the window report, the findings report, the raw scanner report and the working copy of the scanner baseline are opened one folder at a time from the root, and every folder of the chain is held open while it is used:
     - On Windows the folder is held with a share-read handle, so it cannot be renamed, removed or turned into a junction meanwhile. Each opened folder and file must carry no reparse point, and `GetFinalPathNameByHandleW` must name it inside its held parent.
     - On POSIX each folder is opened with `O_DIRECTORY | O_NOFOLLOW` relative to its parent's descriptor, and each file with `O_NOFOLLOW`.
     - Reads and writes go through that handle only. Writes are in place (the ledger under its lock, flushed and synced) or create a new file, never a rename. A link refuses before publication, or fails the write after it; it is never followed.
   - **The raw scanner report** goes into a folder the window creates for that scan and holds until the report is read. Cleanup deletes the entries inside that same folder, then the folder itself after checking that it is still the object the tool created. Anything left refuses, with its name.
   - A recorded signal is checked after these reads, immediately before the switch (fix round 2, RT2).
   - An already-public start is left public and reported with a loud warning (S35).
   - An internal repository is left untouched.
4. **Dispatch and wait.**
   - Compare the whole remote ref map with the scanned one again; a difference here means a push raced the switch, and the tool returns to private at once. Then run `gh workflow run <file> --ref <branch>`.
   - Tie the run to the commit: take the run id from the printed run URL. Otherwise exactly one new `gh run list --commit <sha> --event workflow_dispatch` id must appear within `dispatch_lookup_timeout_seconds`.
   - Then `gh run view` must show `headSha` equal to the commit and `event` equal to `workflow_dispatch`.
   - Poll every `poll_interval_seconds` until `status` is `completed` or `run_timeout_seconds` passes.
5. **Always-run return.** A `finally` path runs on success, red, timeout, any error and any signal.
   - **Signals are recorded, never raised.** From before the first change until the report and the exit-3 message are out, SIGINT, SIGTERM, SIGHUP, SIGQUIT and SIGBREAK (whichever the platform defines) only set a flag. The window stops at its next step boundary. The boundaries are: after the preconditions; after the last private read, immediately before the switch; after the switch; before the dispatch; each run-lookup iteration; each poll. The wait is therefore the longest single step in progress: one `git` read, one `gh` call, one poll sleep, or the scanner during the preconditions. CPython runs a handler "at a later point (for example at the next bytecode instruction)", and an exception from it "may be raised after any bytecode instruction" (`Doc/html/library/signal.html`, 3.12), so no handler that raises is active anywhere in the window.
   - Child processes run in their own session (`start_new_session`, POSIX; `CREATE_NO_WINDOW` on Windows), so a terminal Ctrl+C or hangup reaches only the tool and cannot kill a `gh` call of the return.
   - It switches back to private and reads the visibility again, up to `restore_attempts` times. A failure of any kind in one attempt is recorded and the next attempt runs.
   - If the read does not show private, it exits 3 and prints `REPOSITORY MAY BE PUBLIC` with the command to fix it.
   - Only after the private state is verified, a timed-out or interrupted run is cancelled, best effort. A failed cancel is reported and never changes the exit code.
   - A hard kill skips this path (see *Known limits*).
6. **Report.** A JSON file under `report_dir` (`runs/ci-window/`, Git-ignored) records the result, run id and URL, the visibilities found and left, errors and warnings.

**Exit codes:** 0 green; 1 red, cancelled, interrupted, timed out or an error inside the window; 2 refused before anything changed; 3 the visibility is not back.

A red run is fixed while the repository is private; the orchestrator commits and pushes the fix, and the window is then repeated.

### Known limits

- **The inventory gate blocks every later window** while an unknown workflow run or artifact is retained: one this tool did not dispatch, or did not record. The same holds while a release, an issue or a pull request exists, or the wiki, discussions or Pages are on. **The tool never deletes anything** and never changes a setting to pass the gate; a person decides what to remove or turn off (PM decision pending user review; the PM tells the user before the first window).
- **Accepted residual:** the logs and artifacts of the tool's own earlier runs become public again in each window: "Actions history and logs will be visible to everyone." ([visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)). Also accepted: forks made during the window ("Anyone can fork your repository."), the activity feed, and the repository's name, description and topics.
- **Not gated, unverified:** packages, Projects, security alerts and draft advisories. The visibility page does not name them; they are assumed to keep their own visibility or role gate (not re-read).
- **Single writer.** During a window only the orchestrator pushes to the remote or changes its content (decision 77); no other clone, agent or automation pushes, creates a release, issue or pull request, or starts a workflow run. This covers the inventory too: a change after its last read is not seen. A push after the last check while private and before GitHub applies the switch cannot be stopped by a local check, because `git ls-remote` cannot freeze the remote. The step 4 comparison detects it, and the tool returns to private at once and reports it; the exposure has already happened.
- **Cancellation after the final checkpoint** (PM decision pending user review, T-022 fix round 3). A signal can arrive after the last cancellation check and before the public edit, or while that request is in flight; no local check can close that gap, because a check cannot recall a request already sent. The guarantee is: no public edit starts after a cancellation is observed at the final checkpoint, and the always-run restore returns the repository to private for any later signal (test `test_a_signal_after_the_final_check_is_returned_by_the_restore`).
- **POSIX: the scanner's own write.** POSIX has no way to hold a folder in place, so a local process could rename the raw folder during a scan and put a link at its name. Gitleaks writes its report by path, so its report could then land outside the project. The tool never reads that report and never deletes through the link: it reads through its own descriptors, and it refuses when the owned folder is gone or replaced, naming the folder it left. On Windows the held folder rules this out.
- **Held folders on Windows.** While a folder of a chain is held (milliseconds for the ledger and the reports; the scan time for the raw report), another process's `os.replace` into that folder, the project root included, fails with a sharing violation. The window then holds the root, `.tmp`, `.tmp/ci-window` and its `raw` folder.
- **A ledger cut by a crash** during its in-place write is empty or torn. Empty reads as no runs, torn refuses, and either way the next window refuses any retained run until a person repairs the ledger.
- **Hard kills run no code:** SIGKILL, `TerminateProcess` (`taskkill /F`, `os.kill` on Windows, a job kill), power loss, an interpreter crash, and on Windows a console close, logoff or shutdown. The last three are treated as hard kills (PM decision pending user review, T-022 fix round 1), because the system ends the process right after the console handler returns. A restore needs up to `restore_attempts` × `gh_timeout_seconds`. Run the exit-3 fix command by hand; phase 2 adds a privacy check at every session start.
- **Assumptions,** not re-read on a primary page (no network in that round):
  1. On Windows, the C runtime maps console close, logoff and shutdown to `SIGBREAK`, and the system ends the process when the console control handler returns.
  2. A child started with `CREATE_NO_WINDOW` has no handle to our console, so our console's Ctrl+C does not reach it.
  3. On Linux, a terminal Ctrl+C or hangup goes to the whole foreground process group. That is why the children run in their own session.

## Secret scanner

gitleaks was chosen because it scans Git history. **The user approved it and its download into the project (decision 113).**

**Installed:** the PM installed 8.30.1 for Windows x64 at `.tmp/tools/gitleaks/8.30.1/bin/gitleaks.exe`.
- SHA-256: `17157e2ee8b76fc8b1d8bee607a250e34b8a8023c8bc81822d4b5ee4d78fcb7c`.
- The zip matched the release's checksums file.
- `gitleaks.exe version` → `8.30.1`.

`secret_scanner` holds one entry per `sys.platform` value, because the binary is per platform. Each entry is either `pin_required` or an object with:
- `executable`: relative to the project root, and contained in it after resolving links;
- `sha256`: checked before the scanner runs and again after the scan, so the bytes that ran are the pinned ones;
- `version`: must equal exactly what the binary prints for `version_args`. The output must be one UTF-8 line; one line end is allowed. Anything else refuses and names both values; the command's stderr is never shown;
- `version_args`: the arguments that make the binary print its version (gitleaks: `["version"]`); they may not hold `{revs}` or `{repo}`;
- `args`: the token `{revs}` is replaced by the commits of every remote ref, and `{repo}` by the working tree.

`win32` is filled. `linux` is `pin_required`, so a window run from Linux refuses and names the missing entry.

**The scanner's exit must agree with its report:** 0 with no finding, 1 with findings. Any other exit or combination refuses. Findings are then matched against the baseline below, and any finding outside it refuses.

**First real scan** (2026-11-15, read-only, before the baseline): it found 2 findings in `6b33f75` and refused. They were then listed with `findings` and baselined after review (below).

**Nothing in the repository or environment may weaken the scan.**

`gitleaks --help` (8.30.1) gives where it reads its config from: "1. --config/-c", "2. env var GITLEAKS_CONFIG", "3. env var GITLEAKS_CONFIG_TOML", "4. (target path)/.gitleaks.toml". It also reads a `.gitleaksignore` from `--gitleaks-ignore-path`, whose default is ".".

Passing our own `--config` would mean writing rules, so the tool refuses instead in these cases:
- `.gitleaks.toml` or `.gitleaksignore` exists in the scanned tree, tracked or not;
- `GITLEAKS_CONFIG` or `GITLEAKS_CONFIG_TOML` is set.

The shipped args add `--ignore-gitleaks-allow`, so an inline `gitleaks:allow` comment cannot hide a finding either.

**`.tmp/tools` is kept by the sweep.** In main, `purge_tmp.py` lists `(".tmp", "tools")` in `PROTECTED_PAIRS`. If the binary is still missing, the hash check fails and the window refuses; nothing passes unscanned.

### Seeing the findings without the secrets

```
<venv python> -B tools/agent-workflow/ci_window.py findings --repo <tree> [--commit <full sha> ...]
```

**What it scans:** the commits of every remote ref (`git ls-remote`), or only the commits given with `--commit`. It applies the same refusals as the window.

**How it runs the scanner:** with `--redact --report-format json --report-path`, writing the raw report under `findings_raw_dir` (`.tmp/ci-window/raw`).

**What it keeps:** only `RuleID`, `File`, `StartLine`, `Commit` (full SHA) and `Date` for each finding.
- It writes these to `findings_dir` (`.tmp/ci-window/findings/findings-<time>.json`).
- It deletes the raw report on every path. A raw report that cannot be deleted is a refusal.
- It prints a table of those fields.

The `Secret`, `Match` and `Line` fields, the raw file, and the scanner's own output are never printed or kept. A report it cannot parse is refused, and the error names the field, never the text.

**Exit codes:** 0 when no finding is outside the baseline (baselined findings are still listed); 1 when any finding is outside it; 2 refused.

**First real run** (2026-11-15, `6b33f75`, read-only):

| RuleID | File | StartLine | Commit | Date |
| --- | --- | --- | --- | --- |
| private-key | tools/agent-workflow/tests/test_recall.py | 180 | 1adb1eda51b4 | 2026-11-11T14:48:25Z |
| generic-api-key | tools/agent-workflow/tests/test_recall.py | 169 | 1adb1eda51b4 | 2026-11-11T14:48:25Z |

The PM classified both as test data (below).

### Baseline of reviewed findings

The file is [`tools/agent-workflow/gitleaks-baseline.json`](../tools/agent-workflow/gitleaks-baseline.json). Its path and SHA-256 are pinned in `ci_window_config.json` `scanner_baseline`.

**How a finding is suppressed:** only when its gitleaks fingerprint is an entry of the baseline. The fingerprint is `commit:file:rule:line` (gitleaks README v8.30.1: "Fingerprint: cd5226711335c68be1e720b318b7bc3135a30eb2:cmd/generate/config/rules/sidekiq.go:sidekiq-secret:23"). The fingerprint contains the commit, so an entry covers that one line of that one commit, and nothing later.

**The pin covers the committed blob `<commit>:<path>`.** The window reads it from the scanned commit's object id, so a later move of HEAD cannot change it; `findings` reads `HEAD:<path>`. Git normalises the blob, so a CRLF checkout does not change the hash.
- The working file must parse identical to the blob.
- Any change, a new entry included, refuses until the pin is changed. That change shows up in review.
- Each entry has exactly `RuleID`, `File`, `StartLine`, `Commit`, `Fingerprint`, `Reason` and `Decision`. The `Fingerprint` must equal its fields.

**Rule:** every entry needs a reviewed reason and a decision recorded in `tasks/DECISIONS_INDEX.md`. A real secret is never baselined: it is rotated, and that history is never made public.

**A stale entry** (its fingerprint no longer occurs) prints a warning and does not refuse. Findings are tied to a commit, and pushed history is never rewritten (AGENTS.md section 11), so an entry that matches nothing can hide nothing. Remove it in review.

**gitleaks' own `--baseline-path` is not used** (deviation from the PM's request, round 4). The experiment:
- with entries holding the fingerprint fields, the real 8.30.1 suppressed neither reviewed finding;
- its README says only "A baseline can be any gitleaks report", not which fields it compares;
- matching on more fields would copy commit author data and other raw-report fields into a tracked file.

The tool therefore matches the fingerprint itself, in one scan run.

#### Baseline entries

| Fingerprint (commit:file:rule:line) | Reason | Decision |
| --- | --- | --- |
| `1adb1eda51b4…:tools/agent-workflow/tests/test_recall.py:generic-api-key:169` | Test data: the fake API-key assignment that `test_redact_removes_each_secret_class` feeds to `recall.redact` to check that it is removed | PM decision pending user review (decision 94), 2026-11-15 |
| `1adb1eda51b4…:tools/agent-workflow/tests/test_recall.py:private-key:180` | Test data: a stub key block in `test_redact_private_spans_and_key_blocks_keep_line_numbers`, which checks that `recall.redact` removes key blocks | PM decision pending user review (decision 94), 2026-11-15 |
| `e9b2781a19ff…:docs/ci.md:generic-api-key:197` | Documentation, not a secret: this table's first row quoted the same fake test value in commit `e9b2781`; the row now describes it instead, so later commits do not carry it | PM decision pending user review (decision 94), 2026-11-15 |

**Real run with the baseline** (2026-11-15, read-only, commit `6b33f75`):
- `findings`: 2 findings, both in the baseline; none outside it, none stale.
- The scan gate: `secret scanner 8.30.1 clean over 1 commit(s), 2 finding(s) all in the baseline of 2`.

## Risks the user accepted and how the procedure limits them

| Risk | Limit |
| --- | --- |
| Everything in the history is world-readable while public and may be copied; "Actions history and logs will be visible to everyone." (N134) | The step 1 checks over every remote ref; the step 3 inventory gate on the non-Git surfaces (only the tool's own runs and artifacts may exist); the window lasts only as long as the run |
| Going private: "Stars and watchers for this repository will be erased, which will affect repository rankings." and "Current forks will remain public and will be detached from this repository." (N134) | Accepted by the user; the observed effect is recorded after the first window |
| Going public: "All push rulesets will be disabled." (N134) | None configured today (*unverified*: not checked with `gh`, which only reads were allowed) |
| A crash between steps 3 and 5 leaves the repository public | Signals recorded, never raised, so every signal reaches the `finally` return; the step 4 time limit; exit 3 with the fix command; for hard kills, (phase 2) the session-start check |
| A push lands after the last check while private and before the switch | The single-writer rule; the step 4 comparison returns to private at once (*Known limits*) |

## Billing

"The use of standard GitHub-hosted runners is free:" … "In public repositories". "For private repositories, usage is measured against two parts of your plan". Windows jobs cost more than Linux jobs (N134). A run inside the window is a public-repository run.

## Not decided (proposal)

Whether Windows runs in every window, or only at feature completion and before a release. Decision 26 allows both, and `tasks/STATUS.md` records "both runners on every run" as the current default. No run time has been measured yet. The proposal is to keep both runners in every window, and to revisit after the first measured run, because the Windows leg is free while the repository is public.
