# P3-SWEEP-1: accepted P3 follow-ups (KBS-F3, T-016, K4-ALIASFAIL, CI-SHARD, test hygiene)

Status: **DONE except item 2 (stopped).** Kind: code. R-IDs: R-014, R-015, R-017, R-025, R-026.
Interpreter: the main tree's `.venv` (`.venv/Scripts/python.exe` under the project root), `PYTHONPATH` = this worktree's `src`, every command through `test_slot.py`, `--basetemp .tmp/P3-SWEEP-1/pytest`, one process at a time.

## Step 0 (builder)

Small task (`Small: yes`), so Step 0 sits here and work continues. Unknowns: (a) the shared loader is `config.py`, on the do-not-touch list (item 2); (b) `storage/store.py` is excluded, yet item 1c needs to know which listed file is damaged; (c) whether the store can tell an identical publish (item 6). Proposed answers: (a) stop item 2 and write why; (b) classify inside `intake.py` from the current manifest, no `store.py` edit; (c) yes, from the registry summary (`sync_identity`, `k4_failed`) plus the current snapshot's `aliases` rows, no new constant. Plan: items 4, 1, 3, 5, 6, 8, 7, 9, then mutants and test sets; finish when every item is done or stopped with a reason.

## Items

(Per item: finding, root cause, fix, test, mutant. Filled below as the items finish.)

### Item 1 KBS-F3 tests and the K5 message
- **Finding:** (a) no test forced a manifest `images/<hex>` entry whose sha256 differs from `<hex>`; (b) no test of the bounded copy read; (c) a damaged K5 index file (`index/*.arrow`, a manifest `file` entry) makes `pin()` raise `RetainedFileError`, which intake maps for attach requests to `RETENTION_MESSAGE` ("image copies unavailable"), naming the wrong cause.
- **Root cause:** (a), (b) coverage gaps of KBS-F3; (c) `RetainedFileError` is raised for every manifest `file` kind (copies, marker, index) and carries no entry, so intake had one message for all.
- **Fix:** `intake.py`: `_damaged_file_error(store)` reads the current manifest in `pin`'s order, finds the first listed file whose size differs and answers by its place: `index/...` -> `KnowledgeIndexDamaged(INDEX_DAMAGE_MESSAGE)` ("knowledge index file missing or changed; run kb sync to republish"), anything else or an unreadable/changed manifest -> the old retention message. Only fixed strings leave (no path, R-014/R-026). It uses the store's private `_manifest`, `_entry_path`, `_snapshot_dir` because `store.py` is excluded (see Noticed).
- **Tests (`tests/test_retained_images.py`):** `test_manifest_copy_entry_whose_hash_differs_from_its_id_is_refused_at_intake`, `test_retained_copy_larger_than_its_manifest_size_is_refused_without_reading_it` (no `os.read` at all), `test_retained_copy_growing_during_the_read_is_never_read_past_its_bound`, `test_damaged_k5_index_file_is_reported_as_an_index_problem_not_as_missing_image_copies[missing, wrong_size]` (message, no tmp path, no lease left), `test_damaged_k5_index_file_is_reported_without_attachments_as_before`. Helper: `tests/bounded_read_fixtures.py` (labelled, hook-driven growth).

### Item 2 T-016 D6 (loader link rule): **STOPPED**
- **Why:** the shared loader is `config.py` (`_inside`, `_resolve_within`, `_load_chain`, `_lineage`, `_schema_path`, `load_profile`), and `config.py` is on the brief's do-not-touch list (T-016 P4 changes it now). `_inside` is also used by every `_configured_path` caller, so a no-link rule there changes more than the four path kinds and needs its own design and review against T-016 P4. Nothing was changed for this item; no wrapper outside `config.py` can reach `extends`/`overlay_of`/profile/schema resolution, which happens inside it.
- **For the PM:** make it a follow-up after T-016 P4 integrates: add the no-link rule to `_inside` (or a strict variant used by `_load_chain`, `_lineage`, `_schema_path`, `load_profile`), reuse the CLI wording from `cli_input.resolve_input` ("is a link or another spelling of a stored name"), remove the CLI duplicate only once the loader covers it.

### Item 3 request file growing during the bounded read
- **Finding/root cause:** `read_bounded` (T-016) relies on `read_source_file`'s per-chunk bound; no test showed a file that grows mid-read. Coverage gap.
- **Tests (`tests/test_cli_inputs.py`):** `test_read_bounded_refuses_a_request_file_that_grows_during_the_read[10-4096, 20-4096, 100-5]` (refused as "larger than" or "changed while it was read"; the reader never receives more than limit+1 bytes) and `..._through_the_command` (exit 2, argument named, no output). The growth is a hook on `os.read` (first read returns, then the file grows): no sleep.

### Item 4 `attachments._expiry` unbounded read
- **Finding/root cause:** `_expiry` read the index with `real_inside(...).read_bytes()`: no size bound, while `read_attachment` uses `_read_once(..., MAX_INDEX_BYTES)` (bound gap left by T-016).
- **Fix:** `_expiry` uses `_read_once(store, ..., MAX_INDEX_BYTES)`; an oversized index raises `SourceTooLarge` (an `OSError`), which the sweep already counts as failed and leaves the set in place. Class sweep: no other file read in `attachments.py` (the rest is `os.listdir`, anchored publish/remove).
- **Test:** `test_expiry_reads_an_index_within_the_bound_and_counts_an_oversized_one_as_failed` (no byte read for the oversized index; the other expired set is still removed in the same sweep).

### Item 5 K4-ALIASFAIL P3-2 tests
- `test_a_pinned_reader_serves_a_snapshot_that_recorded_failed_keys` (K5 `ServingIndex` over the pinned snapshot with `k4_failed`; the pin keeps its view while a repaired snapshot is published) and `test_every_concept_failing_publishes_author_aliases_only` plus `..._reaches_the_command_with_exit_0_then_reports_the_skipped_publish` (all keys fail: published, no generated alias, author aliases kept, exit 0).

### Item 6 K4-ALIASFAIL P3-1 identical publish (PM decision pending user review)
- **Finding/root cause:** with one K4 key failing every sync republished a content-identical snapshot: no identity check between "build" and "publish" once `k4_failed` disabled the unchanged shortcut.
- **Answer to the brief's question:** yes, without `store.py` and without a new constant: the registry summary already holds `sync_identity`, `sync_layout` and `k4_failed {count, codes}`; the current snapshot's `aliases` table is read through `store.pin()`.
- **Fix:** `kb_sync._publishes_the_same_snapshot` (after K4, before K1 text/tables): same identity and layout, same recorded failures, same alias rows (compared as `(concept_id, alias_text, origin, status, collides_with, language)`) -> return `{'status': 'unchanged', 'publish_skipped': 'identical', 'snapshot_id': current, 'alias_failures': ..., 'report': ...}`, exit 0. The failed keys are still retried each sync (the retry runs before the check). The builder stays `building` (a `failed` registry row after the next writer session, collectable by GC; it is not a LIVE snapshot, so `sync.snapshots.keep` is not consumed). `KB_SYNC_REVISION` is now `kb-sync-3` (the sync identity changes once, so each KB re-plans once without model calls); new pin row appended; the two helpers are in `tests/stage_guard.py`'s kb_sync inventory. `docs/execution.md` updated.
- **Tests (`tests/test_kb_sync_alias_failure.py`):** `test_a_key_that_keeps_failing_is_retried_but_publishes_no_duplicate_snapshot` (replaces the old "published again" test), `test_the_retry_still_publishes_after_a_skipped_identical_publish`, `test_the_identity_check_needs_the_same_failures_and_the_same_alias_rows`.

### Item 7 CI-SHARD per-spawn guard
- **Finding/root cause:** the class guard looked for `"-m", "pytest"` and `child_environ(` anywhere in a file, so one scrubbed spawn covered an unscrubbed one.
- **Fix/tests (`tools/agent-workflow/tests/test_ci_shard.py`):** AST guard `unscrubbed_pytest_spawns`: for each `subprocess.run/Popen/call/check_call/check_output` (or bare import) whose command is a list literal with `-m pytest` or leading `pytest` (also through a variable), the call's own `env=` must be built by `child_environ()` (directly, through a variable assigned in the same function, or through a same-file helper). One named exemption, `DELIBERATE_RAW_ENV` (`test_ci_shard.py::run`, the test that shows the leak), checked not to be stale. Tests: the two-spawns file, 14 spawn shapes, the helper case, the real suites.

### Item 8 stable parametrize ids
- **Root cause:** `DELETE = object()` in `tests/test_k1_config.py` was part of `ids=f'{key}={value!r}'`. **Fix:** a `_Delete` class with `__repr__` `DELETE`. Sweep: `pytest --collect-only -q tests` (8495 tests) has 0 ids containing ` at 0x`.

### Item 9 flaky `test_embed_server_lifecycle.py` over-cap test
- **Root cause (experiment):** `test_an_over_cap_refusal_reaches_the_client_for_bodies_below_at_and_above_the_budget` held a body of up to `max_discard_bytes` to the guarantee "the 503 reply arrives", but the over-cap path drops raw bytes from the socket, so the budget is spent on the request head too (56 bytes). A body of 99944..100000 bytes (hypothesis draws `just(100000)`) exceeds the budget; the server closes with the client's last bytes in flight, the reset can reach a late-scheduled client before it reads, and Windows discards the reply. Probe (removed): a client reading 0.5 s late: total <= budget 0 of 8 empty replies in every size (51, 50055, 99998, 99999, 100000, 100025 total); total past the budget: 1 of 8 empty (100056 and 150056). No sleeps or unbounded waits were the cause (the test holds its slots with `hold_deadlines`, the server waits are bounded).
- **Fix:** the property compares `len(head) + size` with the budget and draws a band around the edge; new deterministic test `test_the_over_cap_budget_counts_the_request_head` (head and body exactly the budget). The route (404) and oversize (413) properties are correct (the handler parses the head first; the budget there counts the body only). Five runs of the whole file: 72 passed each.

## Mutants (one scratch copy of the changed files per mutant; `.tmp/P3-SWEEP-1/mut/`)

| # | Item | Mutation | Result | Killed by |
|---|---|---|---|---|
| M1 | 6 | alias rows `==` -> `!=` | KILLED | `test_kb_sync_alias_failure.py:87` |
| M2 | 6 | alias rows not compared | KILLED | `...:134` |
| M3 | 6 | recorded `k4_failed` not compared | KILLED | `...:131` |
| M4 | 4 | expiry back to unbounded `read_bytes()` | KILLED | `test_expiry_reads_an_index_within_the_bound...` |
| M5 | 1c | K5 damage answered with the retention message | KILLED | `test_retained_images.py:626` |
| M6 | 1c | index prefix swapped to `images/` | KILLED | `...:626` |
| M7 | 1a | copy sha256 vs id check dropped (`store.py` scratch copy) | KILLED | `...hash_differs...` (DID NOT RAISE) |
| M8 | 1b | copy read unbounded (`store.py` scratch copy) | KILLED | `...:560` |
| M9 | 3 | growth bound removed in `k0_scan.read_source_file` | KILLED | `test_cli_inputs.py:231` |
| M10 | 9 | over-cap refusal never lingers | KILLED | `test_the_over_cap_budget_counts_the_request_head` ("51 bytes pending") |
| M11 | 7 | spawn guard back to per-file text check | KILLED | two-spawns and helper tests (2 failed, 14 passed) |
| - | 5, 8 | tests only / id class | not mutated | item 8 is checked by the collect sweep, item 5 by M1-M3 on the same publish path |

The first mutant run was invalid (the scratch copy lacked `contracts/`, `configs/`, `prompts/`, so every run died at "cannot load schema"); `mutant.py` was fixed and all of M1-M10 were rerun (`mutants2.log`). No mutant survived.

## Pre-mortem (decision 169)

| Risk | Covering test |
|---|---|
| The skip hides a real change (new alias, other failure code) and serves a stale snapshot | `test_the_identity_check_needs_the_same_failures_and_the_same_alias_rows`; M2, M3 |
| The skip stops the retry, so a repaired key never publishes | `test_the_retry_still_publishes_after_a_skipped_identical_publish` |
| Skipped build leaves duplicate LIVE snapshots or eats `sync.snapshots.keep` | `test_a_key_that_keeps_failing_is_retried_but_publishes_no_duplicate_snapshot` (LIVE list unchanged, builder `building`/`failed`) |
| `kb-sync-3` breaks the stage pin | `tests/test_stage_revisions.py` (391 passed, new pin row) |
| K5 message leaks a path or the lease stays held | `test_damaged_k5_index_file_is_reported_as_an_index_problem...` (no tmp path, no lease) |
| A damaged index with no attachment changes its old message | `test_damaged_k5_index_file_is_reported_without_attachments_as_before` |
| Classifier misreads a damaged copy as an index problem | M6 and the copy-size test (still `RETENTION_MESSAGE`) |
| Bounded expiry read counts an oversized index as expired and deletes it | expiry test: oversized set counted failed and left in place |
| Growth test depends on timing | hook on `os.read`, no sleeps; M9 |
| Per-spawn guard misses a spawn shape (`Popen`, variable command, helper env) | 14-shape parametrized test, helper test, stale-exemption test; M11 |
| The flake rewrite no longer tests over-cap delivery | the property still asserts 503 for every size; M10 kills a never-lingering server |
| Loader link rule (item 2) | not applicable: item 2 stopped, nothing changed |

## Completion report

**Behaviour:** items 1, 3, 4, 5, 6, 7, 8, 9 done; item 2 stopped (needs `config.py`, excluded). Item 6 changes sync output for a repeating K4 failure: `status: unchanged`, `publish_skipped: identical`, exit 0 (PM decision pending user review; the sync identity changes once with `kb-sync-3`).

**Commands (all through `test_slot.py`, main `.venv`, `PYTHONPATH` = worktree `src`, `--basetemp .tmp/P3-SWEEP-1/pytest`, one file per process):**
- Pipeline files, one process each: `test_retained_images.py`, `test_cli_inputs.py`, `test_kb_sync_alias_failure.py` (19 passed), `test_k1_config.py` (558 passed), `test_stage_revisions.py` (391 passed), `test_embed_server_lifecycle.py` (72 passed, five runs), and the other files of the batch: all passed.
- Collect sweep: `pytest --collect-only -q tests` -> 8495 tests, 0 ids with ` at 0x`.
- Tool suite, separate process: `tools/agent-workflow/tests/test_ci_shard.py` -> 92 passed (60 s); `test_live_state_guard.py` -> 46 passed, 1 skipped.
- Mutants: M1-M11 above, all killed.

**Changed files:** `src/prompt_enhancer/{attachments,intake,kb_sync}.py`, `docs/execution.md`, `tests/{stage_guard,test_stage_revisions,test_cli_inputs,test_embed_server_lifecycle,test_k1_config,test_kb_sync_alias_failure,test_retained_images}.py`, `tests/bounded_read_fixtures.py` (new), `tools/agent-workflow/tests/test_ci_shard.py`, this file. Untouched as required: `cli_commands.py`, `config.py`, `__main__.py`, `storage/store.py` (only mutated on a scratch copy).

## Configuration audit

No new configuration leaf. Retained literals: `INDEX_DAMAGE_MESSAGE` (protocol-bound operator hint, like `RETENTION_MESSAGE`), `_INDEX_PREFIX = 'index/'` (artifact-bound, the K5 layout), `MAX_INDEX_BYTES` (existing safety-bound, now also used by the expiry sweep), the `'identical'` status word (protocol-bound result field). No profile key read, added or ignored.

## not_run

- Item 2 (loader no-link rule): not implemented, reason above.
- Ruff / lint: not installed, `not_run`.
- Full pipeline suite and full tool suite: not run (one file per process by instruction); only the files above.
- POSIX/Linux run: not run (Windows only here). Link-creation variants of the new tests (symlinks) were not added or run.
- Mutants for items 5 and 8: not run separately (see table).

## Noticed, not done

- `src/prompt_enhancer/intake.py` reaches `KnowledgeStore._manifest/_entry_path/_snapshot_dir` (private) to name the damaged file: a public `damaged_files()` or an entry path on `RetainedFileError` belongs in `store.py` after T-016 P4.
- `tests/probe_over_cap_p3.py` is an empty leftover of a throwaway probe: the tool guard blocks deleting files outside `.tmp/`; the PM removes it.
- Item 2 follow-up after T-016 P4 (see item 2).
- A skipped identical publish leaves one `building` registry row that becomes `failed` at the next writer session and is collected by GC; a store-side "abort build" would be cleaner (`store.py`, later).

## PM integration (2026-11-19)

Review PASS (Claude Sonnet, `p3sweep-rev-sonnet`), intent check clean (R-001, R-004, R-009, R-026), six P3. The PM fixed:
- F3: `INDEX_DAMAGE_MESSAGE` no longer tells the operator to "run kb sync to republish": the reviewer's probe showed sync raises `RetainedFileError` on a damaged current snapshot. It now states only the fault. The same false remedy in the older `RETENTION_MESSAGE` and `store.py:695` is pre-existing (follow-up below).
- F4: `docs/execution.md` now says the next writer session removes the skipped build directory and marks its registry row `failed`, which `kb gc` collects.
- F6: `tests/probe_over_cap_p3.py` is excluded from the patch (staged by pathspec).

Follow-ups (not done here):
- F1: the item 9 over-cap tests cannot tell a smaller server budget from the real one (mutants `-1`, half, `0` survive); add a labelled late-reading client to the exact-budget test.
- F2: the per-spawn guard in `test_ci_shard.py` misses list concatenation, module-level command variables, parameters, aliased `run`, `os.system`, `shell=True` and `asyncio.create_subprocess_exec`. None exists in the repo today; treat a non-literal command as unscrubbed.
- F3b: sync cannot recover from a damaged current snapshot, and `RETENTION_MESSAGE` and `store.py:695` give the same false remedy; fix the recovery in `store.py` or drop the hint.
- F4b: a skipped identical publish still re-reads, re-hashes and re-copies every retained image; run `retain_images` after the identity check.
- F5: the identity check does not compare which concepts failed, the alias `revisions` column or K2/K3 outputs (needs contrived state); add a hash of the sorted failed concept ids.
