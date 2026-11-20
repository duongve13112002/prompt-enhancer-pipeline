# CI42-FLAKE: two timing-dependent CI failures on c3e61d8

Kind: code | R-IDs: R-025, R-009, R-017 | Base: c3e61d8 (detached worktree) | Small: yes | Role: implementer (Claude)

## Step 0 (atop the report, small task: continued)

1. **Unknowns / failure modes:** (a) a shared recorder without a lock; (b) a probe or HTTP timeout under load; (c) a Windows
   file held open by a reader while another thread deletes it; (d) MS-2 changing the likelihood. Hazards for the fix: a longer
   timeout that only lowers the odds; a lock that serialises the pipeline (S46); a Windows-only path that breaks Linux (R-025).
2. **Questions (proposed answers, none blocking):** Q1 may a fixture raise test-only transport timeouts? yes, the module
   already does it for the deadline (`cli_fixtures.py:69-71`). Q2 may the ledger tool retry on `PermissionError`? yes, the
   tool already does (`cli_worker.py:1320`, `hooks/tool_health.py:62`). Q3 `git worktree add` for the base: the guard
   refused it (`git worktree` may only `list`), so the base is a scratch copy, see Reproduction.
3. **Plan:** reproduce both; read the trace of a failing unit; fix each at its cause; sweep the class; mutate; 10 runs.

## Reproduction

Base scratch (not a git worktree; none was created, nothing for the PM to remove except `.tmp/CI42-FLAKE/`): a copy of this
tree with every file that differs between 291d03d and c3e61d8 restored with `git show 291d03d:<path>` (`mkbase.sh`; `src`
differs only in `config.py`, `profiles.py`). Runs: `test_slot.py` → `.venv` python, `-p no:cacheprovider`, a loaded machine
(two to four loops at once, which is what makes the race likely).

| Failure | Tree | Before (unfixed) | After the fix |
|---|---|---|---|
| 1 `test_eval_cli.py` whole file | c3e61d8 | run 1 and 2 green, run 3 FAIL (off-only: `['A plain bowl, no extras'] == [..., 'Une autre']`), run 4 FAIL (concurrency: `2 of 3 requests reached the model double`); loop stopped = 2/4 green | **9/10 green** (`fix-*.log`). Run 7 FAILED with a different symptom: `TimeoutExpired` (600 s) of the **resume** child at `test_eval_cli.py:111` (run took 868 s). None of the three failure-1 symptoms recurred in 10 runs |
| 1 concurrency test alone | c3e61d8 | 6 green, run 7 FAIL (`2 of 3 requests...`) = 6/7 | see above |
| 1 whole file | 291d03d | run 1 FAIL (rows differ at `off-1`), run 2 FAIL (rows differ at `gen-2`), run 3 FAIL x2 (a 600 s child timeout; `2 of 3 requests`) = 0/3 green | n/a |
| 1 concurrency test alone | 291d03d | 5 green, 1 FAIL = 5/6 | n/a |
| 1 diagnostic loop (off-only scenario, up to 40 tries) | 291d03d | mismatch at try 20, try 0 and try 20 in three loops | `not_run` after the fix |
| 2 `test_cli_worker_final.py` (whole file, 32 tests) | c3e61d8 | `...straddling...` alone 10/10 green unloaded (`ledger-*.log`): the window is one 50 ms poll; the deterministic probe below fails at iteration 1 | **10/10 green** (`wfinal-*.log`); `test_git_ledger.py` 83 passed, `test_git_ledger_final.py` 48 passed 1 skipped (Linux-only skip), one run each |
| 1 other users of `pipeline_config`, one run each after the fix | c3e61d8 | not run before | all green: `test_eval_config` 211, `test_cli_commands` 12, `test_cli_config` 70, `test_cli_enhance` 24, `test_cli_inputs` 70+1 skipped, `test_cli_properties` 10, `test_enhance_serve` 21, `test_enhance_serve_lifecycle` 31 |

**Open, not explained: failure 3 (resume child hangs).** `test_a_run_writes_one_result_per_scenario...` timed out its resume
`eval run` (`:111`, nothing left to run) after 600 s in `fix-7.log` (fixed tree) and in `base-3.log` (291d03d, same test).
Both runs were 14 to 16 minutes long, with six to eight pytest processes of my own loops at once (the normal file run is 4 to 5
minutes), so heavy load is the likely trigger, but a 600 s wait with no work is a hang, not slowness. Not investigated (step
budget); not caused by fix 1 (it appeared before it). Needs its own root-cause pass (suspects, unverified: the run lock of the
previous child, the trace/monitor drain at exit).

MS-2 (`git diff 291d03d c3e61d8 -- src`: `config.py`, `profiles.py` only) is not the cause: the 291d03d copy fails the same
way, more often only because it ran under more load. It changes neither cause nor likelihood: neither file is on the
request path of an `eval run` unit (loader and manifest validation).

## Root cause 1 (test fixture, not product): 1 s transport timeouts under load

- The trace row of the failing unit (`read_trace.py` on `bt-probe1/a00/data`) holds `error_type: ConnectTimeout`,
  `attempt.code: HTTP_ERROR`, `latency_s: 3.375`, warnings `INTENT_UNAVAILABLE`, `VALIDATION_FALLBACK`, status `degraded`. The
  planner call never reached the double (`seen`: 4 text + 8 image probes, ONE planner request, one writer request for two
  units), so `Recording.planner` (`tests/eval_fixtures.py:150-161`, locked, appends on arrival) never saw the prompt.
- The CLI child reads `vlm.backends.main.http` from `model_test_profile`: `connect_timeout_s=1, read_timeout_s=1,
  write_timeout_s=1, pool_timeout_s=1, max_retries=0` (`tests/conftest.py:32-33`), probe `timeout_s` 2 (`tests/cli_fixtures.py:76`
  before the fix). `ModelTransport` builds `httpx.Timeout(connect=1, ...)` (`src/prompt_enhancer/model_transport.py:42`), with no
  retry. On a loaded Windows machine the loopback connect or the first byte takes longer; the planner role fails; the pipeline
  falls back to `degraded` with the original prompt. Correct product behaviour for a timed-out call, so no `src/` change.
- Same cause, other symptoms: `2 of 3 requests reached the model double` (a planner connect timed out, the third request never
  arrived; `Gate.wait_for` `tests/serve_fixtures.py:80`), and rows that differ between concurrency 1 and 3 (a held planner
  request outlives the 1 s read timeout only when the third arrives late; the retry-free fallback adds different warnings).
- The recorder is not at fault: `Recording` appends under a `Lock` (`eval_fixtures.py:148-153`); `FullServerDouble` appends
  `seen` and `answers` from handler threads with a bare `list.append`, atomic under the GIL and only read after the run.

## Fix 1 (`tests/cli_fixtures.py`)

`TRANSPORT_BOUND_S = 120` (test-only, equal to `request_deadline_s`) for the `main` backend's connect, read, write and pool
timeouts and for `probe.timeout_s` in `pipeline_config`; every user of `pipeline_config` (CLI, serve and eval tests) gets it.
It removes the dependency, not the odds: no test here measures a transport timeout, so a timeout is now a hang bound, and a
dropped call still fails the existing assertions (the prompt list, `Gate.wait_for`), now only for a real fault. No sleep, no retry, no lock: the
units still run concurrently and the gate still needs `limit` requests inside the double at once (S46 untouched). Linux:
unchanged semantics, only larger bounds.

## Root cause 2 (product defect in the tool): Windows delete of a record a poll holds open

- `hooks/git_ledger.py:514` (`finish`) did `path.unlink(missing_ok=True)` on `pending/<key>.json` while
  `cli_worker.settled_snapshot` → `gl.settle` → `pending` → `_scan_pending` (`:557`, every `POLL_SECONDS`) read the same file with
  `Path.read_bytes()`. Python opens files without `FILE_SHARE_DELETE`, so Windows refuses the delete for the length of that read:
  `PermissionError [WinError 32]`. In the test the PM thread dies after its entry was appended; the pending record stays, the
  settle sees an orphan, and the worker record gets `git_changes` (the `AssertionError` of the CI log). Real in production: `finish`
  runs in a hook process, the poll in the worker launcher.
- Probe (`.tmp/CI42-FLAKE/unlink_race.py`, no-sleep poll against `unlink`): unfixed `unlink` fails at iteration 1 with
  `[WinError 32]`; with the fix 1500 of 1500 pass.
- MS-2 irrelevant (tool code). Same hazard at `_write_new` (`os.replace` of the failure mark, `:398`; removal of the temp name
  `:406`, a second link of the record a poll may hold) and at the two reads (`_scan_pending`, `finish`).

## Fix 2 (`tools/agent-workflow/hooks/git_ledger.py`)

`_sharing_retry(action)`: where `RETRY_SHARING` (`os.name == "nt"`), a `PermissionError` is waited out in `POLL_SECONDS` steps, at
most `SHARING_ATTEMPTS = 40` (2 s, the bound of `cli_worker.REPLACE_ATTEMPTS`), then raised. Used for the unlink in `finish`, the
replace and the temp unlink in `_write_new`, and the two `read_bytes`. It waits for the real condition (the reader closes within one
small read); a handle held for seconds is a fault and is raised, not hidden. POSIX: no retry, a refusal is raised at once.
No lock, no change to the ledger format or to what `settle` waits for.
Tests (`tools/agent-workflow/tests/test_git_ledger_final.py`, prefix `test_ci42_`): the real Windows sharing violation (a held
handle closed by a timer while `finish` runs; skipped on Linux), and portable stand-ins (labelled: a `PermissionError` raised N
times by a wrapper) for each call site, the bound, and the POSIX no-retry rule.

## Class sweep

| Site | Shares the cause? | Action |
|---|---|---|
| `tests/cli_fixtures.py` `pipeline_config`, used by `eval_fixtures.py`, `serve_fixtures.py`, `test_cli_*.py`, `test_eval_*.py`, `test_enhance_serve*.py` | yes (1 s transport timeouts) | fixed once, in the helper |
| `tests/test_pipeline_s0_s6.py:93-99, :215` (in-process double, probe `timeout_s` 2, shared 1 s `http`) | same mechanism, in-process, not observed failing | not changed; noted |
| `tests/conftest.py:32`, `tests/profile_fixtures.py:43` (the 1 s defaults) | source of the cause for any test that talks to a real loopback double | left: other tests measure or never reach HTTP; helpers override |
| `Recording` / `held_double` / `Gate` users (`test_eval_cli.py`, `test_enhance_serve*.py`) | no race in the recorder (locked); they inherit fix 1 through `pipeline_config` | none |
| `test_embed_server_lifecycle.py:79` `Recording(EngineDouble)`, `test_golden_retrieval.py:136` | unrelated classes of the same name, single-threaded reads | none |
| `git_ledger.py:398, :406, :495, :514, :557` | yes | fixed (fix 2) |
| `cli_worker.py:1320` `save` (registry), `hooks/tool_health.py:62`, `precompact_snapshot.py:187`, `tool_monitor.py:114`, `worker_progress.py:143` | had the same retry already | none |
| `cli_worker.py:1228, 1248, 1259, 1266` (registry lock: `unlink`/`replace` of `LOCK` while `lock_identity` of another process reads it) | same hazard class, not observed; `break_stale` swallows `OSError`; `__exit__` `unlink` does not | **Noticed, not done** |
| `cli_worker.py:1817, 1878, 1880, 1893, 2929` (audit files `unlink`/`os.replace`) | same class if another process reads them at that instant | **Noticed, not done** |
| `hooks/ctx_nudge.py:217-252`, `prepush_mark.py:56-70`, `pm_invariants.py:498`, `codex_hooks.py:442-481` | unlink/replace without retry; readers not shown to poll them | **Noticed, not done** |

## Pre-mortem results

- Lowers odds only? Fix 1 removes a 1 s dependency; the bound equals the existing deadline. Fix 2 waits for the release; its
  bound is a fault bound (tested: persistent busy is raised after exactly 40 attempts).
- Hides a concurrency bug? No lock or barrier added; the concurrency test still asserts `peak_one == 1 and peak_three == 3`.
- Breaks Linux? `RETRY_SHARING` is false off Windows (test: one call, raised at once); the Windows-only test is skipped there.

## Configuration impact / audit

No product configuration key changes. Retained literals: `TRANSPORT_BOUND_S = 120` **test-only**; `SHARING_ATTEMPTS = 40` and
`POLL_SECONDS` **safety-bound**; `RETRY_SHARING` **platform-bound** (the three are in `git_ledger.py`, classified in comments).
No user value, so no override or refusal test applies: not applicable.

## Mutation

Scratch copies only (`mut/aw`, `mutt`; scripts `mutate_ledger.py`, `mutate_fixture.py`).

Fix 2, `git_ledger.py`, the `test_ci42_*` tests: 7 mutants, 6 killed.
M1 no retry on the `finish` unlink (killed by the real Windows test), M2 no retry on the poll read, M3 no retry on `replace`, M4 no retry on
the temp unlink, M5 bound off by one, M7 no retry on `finish`'s record read: all killed, each by its own test. **M6 survived**
(`RETRY_SHARING = True` everywhere): equivalent on Windows, where `os.name == "nt"` already holds; the POSIX no-retry rule is
covered by the `RETRY_SHARING = False` half of the bound test, which only a Linux run can contradict (`not_run` here).

Fix 1, `cli_fixtures.py`: a tiny value (0.001 s) stands in for a loaded machine. F1 connect and F2 probe timeout: killed by the off-only
test. F3 read timeout: killed by the concurrency test (105 s). F4 write and F5 pool timeout: survived, equivalent (a loopback request
never waits on either). **F6, the fix removed (the original 1 s values): survived**, because 1 s only fails under load; the fix
therefore has no deterministic test, and its evidence is the trace row (`ConnectTimeout`), the load reproductions before it and
9 of 10 whole-file runs after it with none of the three failure-1 symptoms.

## Done check

NOT MET: `test_eval_cli.py` 9/10 green, not 10/10 (run 7, failure 3 above, unexplained). Met: `test_cli_worker_final.py` 10/10; the
two named CI failures each have a reproduced root cause with file:line and a fix; the class sweep and mutation are above.
Ruff lint and `ruff format --check`: `not_run` (no run made on the changed files).
Cleanup: scratch under `.tmp/CI42-FLAKE/` (the base copy, mutation copies, logs, scripts, and the probe
`.tmp/CI42-FLAKE/base/tests/test_zz_probe_scratch.py`) is left for the PM to remove; no process of mine is running.
