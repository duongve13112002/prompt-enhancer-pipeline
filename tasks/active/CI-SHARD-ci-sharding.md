# CI-SHARD: shard the slow CI jobs

Status: **build round 1 (Claude Sonnet) done 2026-11-19; awaiting a Claude review (decision 91) and the PM's CI window.**

## Task brief

- **Goal (user decision 168):** a CI window finishes sooner. CI window 25 (run 44415756388): Windows pipeline 51 min, Ubuntu pipeline 20 min, Windows tooling 19 min, Ubuntu tooling 6 min. Shard the pipeline suite per operating system (and the Windows tooling suite), target slowest job near 15 min, every test exactly once, nothing skipped.
- **Requirements:** R-025.
- **Out of scope:** new dependencies, test content, the CI window visibility rules.

## Step 0 (Small: yes, continued without waiting)

- Unknowns: CI per-file times (answer: local per-file junit times, assumed proportional; CI window 25 totals match the local totals within 2 % for the Windows pipeline, tooling is 0.74 of local); job overhead (checkout, setup-python, pip install) is not in the window 25 summary (assumption: 2 to 3 min).
- Plan: (1) time every file locally, (2) `ci_shard.py` + weight table, (3) hooks in both conftests, (4) matrix in `ci.yml`, (5) proof tests, mutants, docs. Done check: the partition proof passes, full collection without variables unchanged, per-shard counts sum to the full count.

## Design choice: a collection hook, not a file-list script

`tools/agent-workflow/ci_shard.py` holds a pure `assign(files, weights, default, count)` (longest-processing-time first, ties to the lowest shard index) and two pytest hooks (`pytest_collection_modifyitems`, `pytest_report_collectionfinish`) imported by `tests/conftest.py` and `tools/agent-workflow/tests/conftest.py`. The hook partitions the files pytest actually collected, so the proof of "every test exactly once" is two small facts: every collected file is in exactly one shard (a property of `assign`, tested directly), and a test is kept iff its file is. A script printing file lists would need a second file discovery that could disagree with pytest (`python_files` patterns, nested conftests, `tests/fixtures`) and shell-specific argument handling on Windows (pwsh) and Linux (bash). The pytest command lines in `ci.yml` are unchanged; the shard comes in through `PE_SHARD` and `PE_SHARDS` from the matrix, never through script text. Without them the hook is inert (the local full suite runs everything).

## Weight table source

`tools/agent-workflow/ci_shard_weights.json`: seconds per file, from `.tmp/CI-SHARD/time_files.py` (one pytest process per file through `test_slot.py`, junit testcase time summed): `timing-pipeline.json` (133 files, 7792 tests, 2995 s), `timing-tooling.json` (52 files, 6461 tests, 1538 s), kept under `.tmp/CI-SHARD/`. `default_weight_s` = 9.4 (the median). Local failures during timing: `tests/test_embed_server_lifecycle.py::test_an_over_cap_refusal_reaches_the_client_for_bodies_below_at_and_above_the_budget` (1 failed, 70 passed; not touched); `test_ci_shard.py` timed before the weight file existed (its 50.8 s is kept as its weight; it passes now in 45 s).

## Matrix and forecast

Nine jobs: Windows pipeline 4, Ubuntu pipeline 2, Windows tooling 2, Ubuntu tooling 1. Heaviest-shard weight: Windows pipeline 749 s of 2996 (25.0 %), Ubuntu pipeline 1498 s (50.0 %), Windows tooling 769 s of 1538 (50.0 %). Scaled to window 25: about 12.8, 10, 9.5 and 6 min of test time, plus 2 to 3 min of setup (assumption): slowest job near 15 min. A forecast, not a measurement. `timeout-minutes: 60` and `run_timeout_seconds: 4200` are unchanged: no sharded run exists yet, so a lower value would be a guess; the test `timeout-minutes * 60 < run_timeout_seconds` still holds. They can be lowered after window 26 (PM value pending user review).

## Pre-mortem (decision 169): failure class, then the check that rules it out

| Class | Check |
| --- | --- |
| A test file in no shard | `test_property_every_file_in_exactly_one_shard_deterministic_and_balanced`; `test_real_collection_is_partitioned_exactly_once_for_every_shard_count` (real `tests/` and tooling collections, 1 to 8 shards); manual run below: shard counts sum to the full count |
| A test in two shards | same two tests (disjoint flat lists); `test_hook_shards_join_to_the_full_collection_with_nothing_twice` (child pytest, node ids joined equal the full list, no id twice) |
| A shard that collects zero tests and passes green | hook raises `UsageError` ("selects no test", exit 4): `test_an_empty_shard_fails_instead_of_passing_green`; mutant "empty shard allowed" killed |
| Collection-time imports that differ per shard | the hook only deselects after full collection, so every shard imports every module of the suite exactly as the full run does; manual run: 4 test files with and without variables give 184 passed in each |
| Conftest fixtures or session state a shard loses | fixtures are untouched; autouse `bind_temporary_project` and session `project_root` ran in the sharded runs (test_loader, test_k2_naming among the 4 files: 134 + 50 = 184) |
| Windows path separators or case in the table keys | keys must be posix and relative (`_key_text` refuses `\\` and `./`; test `test_a_malformed_table_is_refused`); `test_the_committed_table_is_valid_and_names_only_real_files_with_their_exact_spelling` compares the keys with on-disk spellings, so a case drift or rename fails; `assign` is invariant under `\\` and `./` spellings (property test) |
| A new test file added later and left out | it takes the default weight and is still assigned (property test mixes listed and unlisted files; the collection line reports "N file(s) at the default weight"); no table update is required for correctness |
| The stress step run twice or never | `test_suite_steps_take_the_shard_from_the_matrix_environment_and_a_guard_refuses_bad_values`: the step's `if` requires Linux, pipeline and `matrix.shard == 1`, it carries no shard variable, and exactly one matrix entry matches |
| A malformed shard value passing | `test_malformed_values_are_refused` (14 spellings), range, half-set and unset-required tests; `test_the_ci_guard_command_accepts_and_refuses_through_the_environment`; `test_a_bad_shard_value_stops_pytest_before_any_test_runs` (7 cases, exit 4, nothing ran); mutants "zero accepted", "half-set accepted", "index >= count" killed |
| A matrix entry missing a shard | `test_the_matrix_lists_every_shard_of_every_suite_once` (shards 1..N all present, one count per leg, no job twice) |
| Poor balance | `test_the_expected_slowest_shard_stays_near_an_even_split` (within 15 % of a perfect split for the real files and each matrix count) and the list-scheduling bound in the property test |

## Completion report

- **Behaviour:** `ci.yml` runs nine matrix jobs from `include` entries; each suite step has `PE_SHARD`/`PE_SHARDS` in its environment; a new step "Refuse a malformed shard" runs `ci_shard.py check`; the exit stress runs once. `fail-fast: false`, full-SHA pins, `timeout-minutes`, `workflow_dispatch` only: kept. `docs/ci.md` and the header comments updated. `test_ci_window.py` workflow test adapted to the `include` matrix and the stress condition.
- **Commands (interpreter `<project>/.venv/Scripts/python.exe`, through `test_slot.py`, one process at a time):**
  - `tools/agent-workflow/tests/test_ci_shard.py`: `69 passed in 45.80s`.
  - `tools/agent-workflow/tests/test_ci_window.py`: `259 passed in 157.39s`.
  - Collection-only per shard, real hook (`.tmp/CI-SHARD/collect_counts.py`): `tests` full 7792; 1/2 4479 + 2/2 3313 = 7792; 1/4 1921 + 2/4 1556 + 3/4 2289 + 4/4 2026 = 7792; tooling full 6461 (with this file); 1/2 3761 + 2/2 2700 = 6461; 1/1 6461 (id lists equal).
  - Representative files (test_envelope, test_assembly_config, test_loader, test_k2_naming): no variables `184 passed`; 1/1 `184 passed`; 1/2 `134 passed, 50 deselected`; 2/2 `50 passed, 134 deselected`.
- **Mutants** (scratch copy, selector tests): 8 of 8 killed: `index >= count`; last ranked file dropped; default weight ignored in ordering; zero/leading zero accepted; tie to the highest index; empty shard allowed; selection inverted; half-set variables accepted.
- **Noticed, not done:** (1) `tests/test_k1_config.py::test_invalid_value_is_refused_at_load[embedding.backend=<object object at 0x...>]` has a memory address in its test id, so ids differ between runs (breaks any tool that compares ids across runs; sharding compares files). (2) `test_embed_server_lifecycle` over-cap test failed once locally under timing. (3) `ci_window_config.json` `_comment` still quotes the old 37 min estimate; left as history, the value is unchanged.
- **not_run:** the real GitHub run (the PM's CI window); ruff lint and `ruff format --check` (ruff not installed); a whole-suite single run (per-file timing was used instead); the weight table was not re-measured after adding `test_ci_shard.py`.
- **Changed files:** `.github/workflows/ci.yml`, `docs/ci.md`, `tests/conftest.py`, `tools/agent-workflow/tests/conftest.py`, `tools/agent-workflow/tests/test_ci_window.py`, new `tools/agent-workflow/ci_shard.py`, `tools/agent-workflow/ci_shard_weights.json`, `tools/agent-workflow/tests/test_ci_shard.py`, this file.

## Configuration impact and audit (R-015, AGENTS.md section 5)

| Value | Class | Validated by | Override test | Refusal test |
| --- | --- | --- | --- | --- |
| `PE_SHARD`, `PE_SHARDS` | protocol-bound (CI protocol values, not profile leaves) | `parse_shard`, `ci_shard.py check`, the hook | `test_valid_values_are_read`, `test_hook_shards_join_to_the_full_collection...` (counts 1, 2, 3, 5) | `test_malformed_values_are_refused`, `test_an_index_beyond_the_count_is_refused`, `test_one_variable_alone_is_refused`, `test_a_bad_shard_value_stops_pytest...` |
| shard counts per leg in the matrix | protocol-bound (CI) | `test_the_matrix_lists_every_shard_of_every_suite_once` | the matrix itself (1, 2, 4) | missing shard fails that test |
| `default_weight_s` = 9.4 and per-file weights | artifact-bound (measured, `.tmp/CI-SHARD/timing-*.json`; only the balance depends on them) | `load_weights` | `test_a_valid_table_is_read`, `test_assign_balances_known_weights...` | `test_a_malformed_table_is_refused` (12 cases), `test_a_missing_key_or_file_is_refused` |
| `WEIGHTS_FILE` path, `schema_version` 1 | artifact-bound / protocol-bound | `load_weights` | n/a | schema version cases |
| 15 % balance bound, Hypothesis ranges, `NO_WINDOW` in tests | test-only / protocol-bound (Windows console rule) | n/a | n/a | n/a |

No hidden default: an unset pair means the full suite (local use), the CI guard step requires both. No defect literal retained.

## Fix round 1 (decision 164; review `.tmp/CI-SHARD-rev/review-claude.md`, VERDICT FAIL)

| Finding | Fix | Test | Mutant (named, killed) |
| --- | --- | --- | --- |
| P1: `test_live_state_guard.py` child pytest inherits `PE_SHARD`/`PE_SHARDS` and lands in an empty shard on Windows tooling 2/2 | new shared helper `ci_shard.child_environ()` (drops both variables, case-insensitive); the child's env is built from it; `test_ci_shard._env` uses it too. Sweep of both suites for `-m pytest`, `sys.executable`, `subprocess`: only `test_live_state_guard.py` and `test_ci_shard.py` spawn a child pytest; the other hits are guard-test strings or non-pytest children (`test_slot`, `ci_window`, git) that load no conftest | `test_a_child_pytest_with_the_variables_set_runs_its_own_tests_only_through_the_shared_helper` (shows the leak: exit 4 with the raw env, passes scrubbed); `test_child_environ_drops_both_variables_and_keeps_the_rest`; class guard `test_every_test_that_spawns_a_pytest_child_takes_its_environment_from_the_shared_helper` (a `child_environ(` call in every file that spawns it). Reviewer's reproduction (whole tooling dir collected, `-k` the child test): no variables `1 passed, 6466 deselected`; `PE_SHARD=2 PE_SHARDS=2` `1 passed, 6466 deselected` | "scrub removed from the helper" killed; "child env fix reverted in test_live_state_guard" killed (it first survived: the guard looked only for the name, which the import line supplies; tightened to a call) |
| P2: both conftests register the hook, so a session over both suites partitions twice and drops tests with exit 0 | per-session state in `config.stash` (`applied`, report lines): the partition is made once over all collected files; the shard line is reported once | `test_two_conftests_in_one_session_partition_once` (2, 3, 4 shards over two synthetic directories with a conftest each: counts sum to the unsharded 24, nothing twice, line printed once). Real: `tests` + tooling in one session, 2 shards: 7376 + 6883 = 14259 = unsharded (before the fix 3515 + 2390 of 14253) | "stash flag removed", "applied flag never set", "report not once per session" killed |
| P3: docs and `ci.yml` header said "proof in every job"; the Ubuntu forecast used Windows weights unsaid | wording: the proof test runs in the tooling jobs, every job uses the same selector code, no run-time cross-check between shards; Ubuntu rows say "Windows weights, an extrapolation" | wording only | n/a |

Commands (through `test_slot.py`, `--basetemp .tmp/CI-SHARD/pytest`, one process at a time): `test_ci_shard.py` `75 passed in 53.28s`; `test_live_state_guard.py` `46 passed, 1 skipped in 6.63s`; `test_ci_window.py` `259 passed in 139.27s`. Collection only, `tests` alone: 2 shards 4479 + 3313 = 7792, 4 shards 1921 + 1556 + 2289 + 2026 = 7792, 0 duplicates, same file set, full 7792. Pre-mortem rows added: an environment leak into a child process (class guard above); a hook applied twice per session (P2 test). `not_run`: the full suites (another session runs a heavy suite), ruff, the real GitHub run. Changed in this round: `ci_shard.py`, `test_ci_shard.py`, `test_live_state_guard.py`, `ci.yml` (header comment), `docs/ci.md`, this file.
