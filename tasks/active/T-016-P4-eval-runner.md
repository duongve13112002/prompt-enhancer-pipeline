# T-016 P4: evaluation runner (`eval run`, `eval import`)

Status: **built and tested with labelled doubles, handed to the PM for review 2026-11-19** (Step 0 approved with five amendments; Q1 to Q5 and the amendments are each a PM decision pending user review). Nothing was run on the real draft set or a live model. Parent task: [T-016](T-016-cli-run-guide.md) (phase table row P4, Q1, decision 154). R-IDs: R-001, R-009, R-015, R-017, R-020, R-024, R-026, R-027. Step 0 below is kept as approved; where the build differs, the Completion report says so (D-a to D-g) and wins.

## Step 0 (builder), condensed; the approved text is archived verbatim in [tasks/archive/T-016-P4-step0.md](../archive/T-016-P4-step0.md)

Facts the design rests on (source-verified when Step 0 was written): S0 pins only the current snapshot, so a named snapshot must equal CURRENT; `eval_scenario.v0` is closed, so the draft layout (five `draft_*` columns) is read through an adapter and never stored; `eval_results` has `run_id, scenario_id, metrics, status, config_hash` with one atomic segment per `append` and a refusal of repeated keys on read; `rag.enabled` is fixed per `Pipeline`, so the off variant is a copy of the profile; edit source bytes come from the snapshot by content id (on) or `--source-images` (off); code identity here is a declared revision constant, never a source hash.

### Unknowns and ranked questions (each with my proposed answer; none blocks the non-dependent phase 1)

1. **Variants: what does "retrieval on and off" run per scenario?** Proposal: a required argument `--variants record|both`, no default. `record` runs each scenario once, in the variant its record states (`draft_rag_enabled`). `both` adds the off variant to every on record (so the judge pack can compare two blinded outputs of one scenario); an off record always runs off only (running a "retrieval off" kind with retrieval on contradicts its key). Result unit is (scenario, variant): `run_id = <identity16>-<variant>` so the key `(run_id, scenario_id)` stays unique and "one durable result per scenario" holds per variant. PM decision pending user review: a pair per scenario doubles the model calls, so `both` is the user's explicit choice per run.
2. **Edit scenario in the off variant has no source bytes** (R-009 forbids the store). Proposal: an optional `--source-images <dir>` (inside the project root, no link on the path, files named by the image-id digest hex, each file's SHA-256 must equal its image id). It is required only when a run contains an off edit; otherwise a missing file refuses the whole set before the first call. The on variant always reads from the snapshot, never from this directory or the knowledge-base tree. The draft has no off edit record (all five `rag_off` records are `generate`), so the real draft needs no such directory.
3. **Where do the summary, judge pack and import records live?** Proposal: results in `eval_results` (one segment per result, existing table); everything else in an explicit sidecar tree `<data_root>/eval/runs/<run_id>/` written only through the anchored atomic writer (`_anchored.atomic_write_bytes`, no raw write; the scanner test `test_storage_anchored_writes.py` guards that). It needs two small `DataStore` additions: `write_sidecar` / `read_sidecar` (path rule of `DataStore`, under `eval/runs/` only) and `iter_rows(name)` that reads segment by segment so resume never holds every result (R-017). No new table column.
4. **How is a failed attempt counted, and is it retried?** Proposal: an operational failure (exception, no envelope) writes **no** `eval_results` row; it is counted `failed` in the summary and logged in `attempts.jsonl` (error class only, no payload), and **is retried on resume** (it was never a successful call). An honest `failed`/`degraded` envelope is a **completed** result with that status (R-024), never `failed` in the runner's count. `skipped` = not selected by an explicit `--split` filter or an off-only rule (reason recorded); `not_run` = selected but not attempted (interrupt, or a variant that needs an unavailable input).
5. **Structural link check alignment.** Proposal: align the planner's mention to the key's mention by **exact string equality** (no normalisation, no list); a key link whose mention is not found is `unaligned`, counted separately from pass and fail; family and negation parts (`draft_link_details`) stay semantic and `not_run` until scored. PM decision pending user review.

Not asked, decided conservatively and marked "PM decision pending user review": **disagreement** = any difference between the two scorers on one rating (no threshold, so no invented value); the rubric dimensions are the Q1 list (constraint survival per constraint id, leakage, under-editing, link alignment, family/conflict, negation), each applicable only where the key states it.

## Design

The full design as approved is in the archived Step 0 (written once, per the rule of one owning place); the built behaviour is described in [G-evaluation](../../docs/stages/G-evaluation.md) and [execution](../../docs/execution.md). Differences from Step 0 are D-a to D-g below.

## Pre-mortem (decision 169): final check, one passing test per row

Module files: `ec` = `tests/test_eval_cli.py` (real entry point, loopback double), `ek` = `test_eval_concurrency.py` (in-process, labelled `FakePipeline`), `ej` = `test_eval_judge.py`, `ep` = `test_eval_properties.py`, `ef` = `test_eval_config.py`.

| # | Class | Result and test |
|---|---|---|
| 1 | resume repeats a call or writes a second row | ruled out: `ec::test_a_killed_run_resumes_at_the_first_missing_result_without_repeating_a_successful_call` (real kill); `ek::test_a_completed_run_makes_no_call_when_executed_again`; mutant M8 |
| 2 | half-written result counts as done | **partly**: the segment is one atomic temp-file-and-rename `append` (existing `test_storage_anchored_writes`), and the real kill test above leaves whole rows only; no fault is injected inside `append` here (gap, see Noticed) |
| 3 | summary counts failed, skipped or not-run as passed | ruled out: `ek::test_a_failed_call_is_counted_apart_marked_and_retried_alone_by_the_next_invocation`; `ep::test_the_survey_counts_add_up_for_any_mix_of_splits_retrieval_keys_and_tasks` (`units == selected + skipped`) |
| 4 | a semantic value without imported scores | ruled out: `ec::test_a_run_writes_one_result_per_scenario_...` (`semantic` and `intervals` are `not_run` with their reasons in every row and the summary) |
| 5 | foreign snapshot, absent image, bad split, scorer under test | ruled out: `ec::test_a_set_the_run_cannot_trust_is_refused_before_any_model_call_and_any_file` (12 cases, no call, no file); `ej::test_a_damaged_score_pair_is_refused_and_nothing_is_written[family under test]` and `[same family]` |
| 6 | the pack reveals the variant | ruled out: `ej::test_the_pack_shows_outputs_under_blind_labels_and_nothing_about_how_they_were_made`; `ej::test_the_swap_is_one_bit_...`; pack lines are closed by `eval_judge_pack.v0` |
| 7 | import accepts extra, missing, duplicate, unknown, out-of-range, wrong pack | ruled out: 18 cases in `ej::test_a_damaged_score_pair_is_refused_and_nothing_is_written` (not one byte of the data root changes), `ej::test_other_refused_imports`, `ep::test_an_arbitrary_score_document_is_judged_by_the_schema_without_any_other_exception` |
| 8 | `agent_judged` reaches a threshold, calibration or fit | ruled out for this code: `ej::test_an_import_unblinds_flags_every_disagreement_and_never_touches_the_results` (no `eval_results` row is added); `eval fit` still refuses (`test_cli_commands`). The grep test of every reader was not written (T-030 owns the readers) |
| 9 | an off record opens the store | ruled out: `ec::test_an_off_only_run_never_touches_the_knowledge_base` (no store directory; `--snapshot` refused) |
| 10 | multilingual text normalised | ruled out: `ep::test_the_adapter_keeps_every_text_field_verbatim` (any Unicode, byte for byte) and the exact prompts seen by the double in `ec` |
| 11 | the set or all results held in memory | **by construction, not measured**: records decoded 16 at a time (`BATCH_ROWS`), results read one segment at a time (`scan_results`); the set's raw bytes are held, bounded by `evaluation.run.max_set_bytes` (D-e); no instrumented-read test (gap) |
| 12 | OS-specific path or separator | Windows run only (junction link test, `Path` throughout); Linux CI is `not_run` here |
| 13 | a set or score file escapes through a link | `--set`: `ec::test_a_set_path_through_a_link_is_refused`; `--scores` and `--source-images` use the same `read_limited`, not tested separately (gap) |
| 14 | a mid-run publish changes the snapshot | ruled out: `ek::test_a_snapshot_that_changed_during_the_run_stops_it_and_stores_no_result_for_that_scenario` |
| 15 (amendment 1) | the concurrency bound is missed or exceeded; rows differ by concurrency | ruled out: `ek::test_the_bound_is_reached_and_never_exceeded_and_every_unit_has_one_result[1,2,3,5]` (no sleep: the fake holds `limit` requests, then yields 200 loop turns); `ec::test_concurrency_one_and_three_give_identical_results_and_the_bound_is_reached_and_never_exceeded` (differential, real double, client limit above the runner's); mutant M9 |
| 16 (amendment 2) | the seed changes the run | ruled out: `ec::test_a_run_writes_one_result_per_scenario_...` (new seed: no call, same `run_id`, no new row); `ej::test_the_same_seed_writes_the_same_pack_and_another_seed_a_pack_with_another_id_and_no_new_result`; `ef::test_the_blinding_seed_is_validated_but_the_run_identity_never_reads_it`; `ep::test_the_run_identity_ignores_the_evaluation_block_and_nothing_else`; mutant M4 |
| 17 (fix round 1) | two processes of one run both call every unit and append a second row | ruled out: `ec::test_a_second_process_of_the_same_run_is_refused_at_once_and_a_rerun_after_the_first_repeats_no_call` (real subprocess held in a model call; the second `eval run` is refused with no call and no row; a rerun after the first ends repeats no call); mutant F1 |
| 18 (fix round 1) | the lock is not given back on an exception, a refusal or Ctrl+C | ruled out for `eval run` and `eval import`: `ec::test_a_refusal_after_the_run_has_started_...gives_the_lock_back` and `ej::test_an_import_gives_its_lock_back_on_every_exit_path` (spy on the explicit release); mutants F2 and F7. Ctrl+C itself is not simulated: it reaches the same `finally` as an exception (gap) |
| 19 (fix round 1) | a repeated `(run_id, scenario_id)` row is counted twice or refused by one reader only | ruled out: `ej` duplicate-row tests (scan, pack writer and import refuse; `DataStore.read` refuses on its own); `ek` stray-result test (`not_run < 0` is a `StorageError`); mutant F3 (the reviewer's R8) |
| 20 (fix round 1) | a trace-capacity refusal is counted as completed or lost | ruled out: `ek` trace_capacity tests (`not_run`, one attempt, `not_run_reasons`, retried on resume, replaced by a later failure); mutant F4 |
| 21 (fix round 1) | an import reads a fixed-size pack whole | ruled out: `ej` streamed-import test (`read_sidecar` made to reject the pack) and configured-limit test; `test_eval_storage` round-trip property; mutant F5 |

## PM answers and amendments (2026-11-19; PM decisions, accepted by the user in decision 170)

Q1 to Q5 of Step 0 approved as proposed. Amendments: (1) concurrency by `evaluation.run.max_concurrency` (user decision 149, S46), one `Pipeline` per variant, bounded queue, single writer task, differential and bound tests; (2) `evaluation.judge.blinding_seed` is not in the run identity (the pack's `pack_id` and key cover it); (3) `prompts/eval_rubric_v0.txt` is a neutral **draft, PM decision pending user review**, never sent to a pipeline model; (4) no `--resume` flag, a manifest that differs is refused; (5) no bounded read of one trace row by `trace_id` exists (`trace_store.py:23-46` lists and loads whole segments), so the link check is `not_run` ("trace row unavailable").

## Completion report

**Behaviour.** `eval run` and `eval import` are served; `eval build` and `eval fit` keep their exit-2 refusals.
- `eval run --set <draft parquet> --variants record|both --split fit|test|all [--snapshot <id>] [--source-images <dir>]` validates every record and every concept and image id before the first call, runs each unit through its own owned `Pipeline` (off: a copy of the profile with `rag.enabled` false, so no store or gateway), stores one `eval_results` row per (scenario, variant), writes `summary.json` and a blinded pack, and resumes by content identity. At most `evaluation.run.max_concurrency` scenarios are in flight in total; one writer task does every append and `after_delivery`. Exit 0 with the summary JSON when nothing failed or is `not_run`; otherwise counts on stderr and exit 1 (the same command resumes).
- `eval import --run-id <id> --scores <file> --scores <file>` validates all files before writing one byte, unblinds, flags every difference between the scorers, labels every value `agent_judged`, writes only to `imports/`, never to `eval_results`.
- Structural metrics only. `not_run`: ID-level link check ("trace row unavailable": `trace_store.py` has no bounded read by `trace_id`), semantic metrics until scores are imported (then pilot-only), every interval.

**Deviations from the approved Step 0.**
- D-a: extra key `evaluation.run.max_source_image_bytes` (a bounded read of an off-edit source image needs its own bound).
- D-b: `max_concurrency` bounds scenarios in flight in total across both variants (not per variant). Fix round 1 withdrew the earlier claim that `max_concurrency <= runtime.trace.queue_limit` keeps the trace store from refusing for capacity (review P3(a): another process or the service can hold slots); the validator rule is gone, and a `LifecycleFull` unit is recorded `not_run` (reason `trace_capacity`) and retried on resume.
- D-c: the run identity also covers the profile id, its system-prompt hash and the role prompt hashes; it excludes the `evaluation` block (and so the blinding seed).
- D-d: sidecar layout is `eval/runs/<rid>/{manifest.json, summary.json, incomplete/<variant>-<hash>.json, run.lock, packs/<pack16>/{pack.jsonl,key.json}, imports/<import16>.json}`. No `attempts.jsonl` (no append primitive exists): an unfinished unit is one atomic marker file holding `disposition` (`failed` or `not_run`) and the error class or reason only; the summary joins markers against durable rows, so a later success clears the marker.
- D-e: the set's raw bytes are held in memory (bounded by `evaluation.run.max_set_bytes`), records are decoded 16 at a time; `iter_rows` became `DataStore.segments` and `read_segment`.
- D-f: the overlay is unchanged: `evaluation` is not an overlay key (`config.py:607`); the new leaves live in the base and dev example profiles. PM decision pending user review.
- D-g: the link check is not built (amendment 5); the metrics layout has `structural.links = {state: not_run, reason}` and no `alignment` field.

**Commands and real output** (every one through `tools/agent-workflow/test_slot.py`, one pytest process at a time, `--basetemp .tmp/T-016d/pytest`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH` = worktree `src` and `tests`):

| file | result |
|---|---|
| `tests/test_eval_cli.py` (end to end, subprocess, loopback double) | 6 passed in 73.6 s |
| `tests/test_eval_concurrency.py` | 7 passed in 2.6 s |
| `tests/test_eval_judge.py` | 27 passed in 7.6 s |
| `tests/test_eval_properties.py` | 8 passed in 2.3 s |
| `tests/test_eval_config.py` | 183 passed in 10.7 s |
| `tests/test_cli_commands.py` (the run and import rows left the exit-2 list) | 12 passed in 38.1 s |
| `test_cli_config`, `test_cli_properties`, `test_loader`, `test_intake_config`, `test_storage_config`, `test_validation_config`, `test_pipeline_config`, `test_enhance_serve_config` | 70, 10, 134, 31, 10, 169, 62, 197 passed |
| `test_embed_server_models`, `test_real_contracts`, `test_model_schemas`, `test_trace_store` | 18, 3, 14, 7 passed |
| `test_storage_anchored`, `_anchored_writes`, `_tables`, `_properties`, `_concurrency`, `_references` | 27, 177, 21, 7, 4, 5 passed (10 skipped for OS reasons, as before) |
| `tests/test_field_contracts.py` | first run 2 failed (528 passed): the class sweep found that the concrete `evaluation.judge.min_scorers: 2` (schema minimum 2) is a protocol constant; both allow-lists in the test now name it (decision 154); the two tests and the evaluation-named tests then passed (15 passed, 1 skipped) |

**Class sweep.** `NOT_SERVED` and `HANDLERS`; `test_cli_commands.py` rows; read-set parametrisations (`test_intake_config`, `test_loader`, `test_storage_config`, `test_validation_config`); `test_field_contracts` allow-lists (fixed); `docs/execution.md` rows 27-29, `G-evaluation.md`, `contracts/README.md`, `prompts/README.md` (new `eval_rubric_v0` entry).

**Changed files.** Source: `cli_commands.py`, `__main__.py`, `config.py`, `storage/{store,tables,_anchored}.py`; new `eval_set.py`, `eval_run.py`, `eval_judge.py`. Contracts: `enhancer_config.v0.schema.json` (optional `evaluation.run`, `evaluation.judge`); new `eval_result_metrics.v0`, `eval_judge_pack.v0`, `eval_judge_scores.v0`. Configs: `enhancer.example.yaml`, `enhancer.dev.example.yaml`. Prompt: new `prompts/eval_rubric_v0.txt` (**draft, PM decision pending user review**, never sent to a pipeline model). Docs as above. Tests: new `eval_fixtures.py`, `test_eval_{cli,concurrency,judge,properties,config}.py`; edited `test_cli_commands.py`, `test_field_contracts.py`.

## Configuration audit

Every row has validated config (`validate_evaluation_settings`), an example profile value, a non-default override test and a refusal test in `tests/test_eval_config.py` (parametrised by leaf: override, "changes only its own value", malformed value, unresolved sentinel at load, missing key), unless the row says otherwise.

| Key or literal | Class | Evidence |
|---|---|---|
| `evaluation.scenario_kinds`, `split_by` | configurable (existing leaves, now consumed) | base example has values; `split_by` accepts `concept` only (protocol-bound set `SPLIT_RULES`, no second value to override) |
| `evaluation.run.max_set_bytes`, `max_source_image_bytes` | configurable | base `deploy_required`, dev example value; refusal of an oversize file in `ec::test_a_set_the_run_cannot_trust...` |
| `evaluation.run.max_concurrency` | configurable (decision 149) | base `deploy_required`, dev `2`; `ef::test_one_scenario_at_a_time_is_not_a_hidden_default`, `ef::test_no_setting_claims_to_guarantee_trace_capacity` (the `queue_limit` rule is gone), bound tests `ek` and `ec`; mutant M9 |
| `evaluation.run.max_sidecar_bytes` (new, fix round 1) | configurable (replaces the literal `MAX_SIDECAR_BYTES`) | schema integer >= 1 or sentinel, required; base `deploy_required`, dev `67108864`; in the read set of `eval run` and `eval import`; override, "changes only its own value", malformed, unresolved-sentinel and missing-key rows in `ef` (parametrised by leaf, plus the both-commands sentinel test); behaviour: `ej` streamed-import and configured pack size limit tests (reader and writer), `test_eval_storage` exact byte limit; mutant F5 |
| `storage.lock` read by `eval run` and `eval import` (fix round 1) | configurable (existing key, newly consumed by the run-lock constructor; only `try_acquire` is used, so no wait is ever taken) | `ef` read-set test, resolution and override test (2.5 and 0.5); `test_eval_storage` (the run lock needs the lock setting) |
| `evaluation.judge.blinding_seed` | configurable | base `deploy_required`; changes only the pack (`ej`, `ec`) |
| `evaluation.judge.score_min`, `score_max` | configurable | scale must have min < max; a pack written for another scale is refused at import (`ej::test_other_refused_imports`) |
| `evaluation.judge.min_scorers` | concrete `2`, protocol-bound (decision 154: two scorers of different families) | schema minimum 2; override 3 and 9, refusal of 1; allow-listed in `test_field_contracts` |
| `evaluation.judge.under_test_families` | configurable declared identifier list, compared exactly (no word list) | `ej` family-under-test refusal; mutant M2 |
| `evaluation.judge.rubric_prompt_id` | configurable | resolved like a role prompt id; `ec` reads the rubric text and hash into the pack header |
| `evaluation.judge.max_score_file_bytes` | configurable | oversize file refused (`ej::test_other_refused_imports`) |
| `EVAL_RUN_REVISION`, `RUN_DIR`, schema names, `DRAFT_FIELDS`, `SPLITS`, `IMAGE_ID` | artifact-bound | declared in the module with the class |
| `LABELS` `AB`, `EVIDENCE` `agent_judged`, `VARIANTS`, `NOT_RUN_*` reason texts | protocol-bound | PM answer Q1 and decision 154 |
| `BATCH_ROWS = 16` | safety-bound (records decoded per batch; a memory bound of the runner's own read, no result depends on it) | no test; a candidate for a profile leaf if the user wants to tune it (see Noticed) |
| `INCOMPLETE_DIR`, `TRACE_CAPACITY`, `NOT_ATTEMPTED` (fix round 1; the unused `LOCK_NAME` and `DISPOSITIONS` removed at PM integration, the lock file name is the artifact-bound literal of `DataStore.run_lock`) | artifact-bound (directory and file names, marker values) and protocol-bound (reason texts in the summary) | declared in `eval_run.py` with the class; marker tests in `ek` |
| the former `MAX_SIDECAR_BYTES = 1 << 26` | **was a defect (hidden literal, review P3(d))**; now the configurable row above | |
| `YIELDS`, `FAILSAFE_S` in `test_eval_concurrency.py` | test-only | hang guard, no result depends on it |
| no defect literal remains | | |

Not consumed, so not read: `evaluation.min_items_per_class`, `languages` (`COMMAND_READS` narrowed; `ef::test_the_consumed_leaves_are_the_read_set_of_each_command`).

## Mutants

Each applied to the source, run against the named tests, then reverted (checked by grep afterwards).

| # | Mutant | Killed by |
|---|---|---|
| M1 | import accepts `low <= v < high` (top of the scale refused) | `ej::test_the_lowest_and_the_highest_value_of_the_scale_are_accepted` (this test was added after a first review of the set) |
| M2 | refuse only when *all* scorers are of a family under test | `ej::...[family under test]` |
| M3 | a disagreement needs more than two distinct values | `ej::test_an_import_unblinds_...` and the boundary test |
| M4 | `pack_id` ignores the item hashes | `ej::test_the_same_seed_writes_the_same_pack_and_another_seed_...` |
| M5 | `max_concurrency >= queue_limit` refused | withdrawn in fix round 1 (the rule was removed; its test is now `ef::test_no_setting_claims_to_guarantee_trace_capacity`) |
| M6 | repeated constraint id accepted | **survived at first** (no case); `ep::test_every_rule_of_the_record_check_refuses_with_its_own_words_...` added, then killed |
| M7 | literal check inverted (`not in`) | `ec::test_a_run_writes_one_result_per_scenario_...` |
| M8 | resume filter removed (every unit runs again) | `ec` first test and kill-and-resume test |
| M9 | concurrency bound + 1 | **survived at first** (the client's own limit equalled the runner's and the gate released at the bound); fixed by raising the client limit in `evaluation_values` and adding the in-process `ek` test; then killed (`peak 2 == 1`) |

## Fix round 1 (decision 164; review `.tmp/T-016d-rev/review-claude.md`, VERDICT FAIL)

| Finding | Root cause | Fix | Test | Mutant |
|---|---|---|---|---|
| P1 two `eval run` of one identity both call and append a second row | nothing excluded a second writer of one run directory | non-blocking exclusive run lock `eval/runs/<rid>/run.lock` (`DataStore.run_lock`, `eval_run.hold_run`) taken in `prepare` before the first write and released in `release` on every exit path; `eval import` takes it with `create=False`; a held lock makes the next process refuse at once; every sidecar write happens under it; the resume hint says to rerun only after the first process has ended | `ec::test_a_second_process_of_the_same_run_...` (real subprocess); `ej` import refused while the lock is held and works after; `ek` lock tests; `test_eval_storage` | F1 lock not taken; F2 lock not released (run); F7 import takes no lock |
| P2 a repeated result was never caught by a test | the repeated-key check had no test | tests for scan, pack writer, import and `DataStore.read`; `refresh_summary` refuses `not_run < 0` or reasons above `not_run` | `ej` duplicate-row tests; `ek` stray-result test | F3 repeated-key check removed (R8) |
| P3(a) `max_concurrency <= queue_limit` does not prevent trace-capacity refusals | the claim ignored other holders of trace slots | rule and claim removed from validator and docs; `LifecycleFull` is a `not_run` marker (reason `trace_capacity`), no row, retried on resume, counted in `summary.not_run_reasons`; no retry loop, no new wait constant | `ek` trace_capacity tests; `ef::test_no_setting_claims_to_guarantee_trace_capacity` | F4 `LifecycleFull` counted completed |
| P3(b) `min_scorers` listed as protocol-only | the allow-list hid a configurable key | removed from both lists in `test_field_contracts`; candidate value 3 added | `test_field_contracts` (532 passed) | n/a |
| P3(c) blinding cannot hide the variant of a one-output item | a single output carries its own `kind`; the pack cannot fix that | **limit documented** (`G-evaluation.md`, `eval_judge_pack.v0` `$comment`): blinding protects the comparison between the two variants of one scenario; a one-output item (off-only record, `--variants record`) is no blinded on-against-off comparison. **PM decision pending user review** (conservative reading: document, do not drop the one-output item) | `ej` two-output item carries nothing that reveals the labels | n/a |
| P3(d) import read the pack whole under a fixed 64 MiB cap | hidden literal (R-015) | new key `evaluation.run.max_sidecar_bytes`; pack streamed line by line (`DataStore.iter_sidecar_lines`, `k0_scan.iter_source_file`); pack, key and import record refused above the cap before any write | `ej` streamed-import and configured-limit tests; `test_eval_storage`; `ef` key rows | F5 cap back to a constant |
| P3(e) dev comment said "per variant" | wrong comment | both examples now say in total, both variants together | n/a (comment) | n/a |
| P3(f) a refusal after results were written exited 2 | `Refusal` mapped to exit 2 anywhere | after `prepare`, `Refusal` becomes `RunAborted` and exits 1 (`failed: ...`); exit 2 only before any side effect | `ec::test_a_refusal_after_the_run_has_started_...` | F6 refusal after `prepare` passes through (exit 2) |

Class sweep result: every durable writer (rows, manifest, markers, summary, pack, key, imports) runs while the run lock is held; a second `eval run` and an `eval import` of a held run refuse with no side effect; the lock is released on normal, refusal and exception exits; every row counter (summary, import, pack writer) refuses a repeated key; the remaining fixed literals are classified in the audit above. Lock test helpers: `eval_fixtures.SpiedLock` and `spy_on_locks` (labelled test helpers). Touched or new files this round: `k0_scan.py`, `storage/store.py`, `config.py`, `eval_run.py`, `eval_judge.py`, `cli_commands.py`, `enhancer_config.v0.schema.json`, `eval_judge_pack.v0.schema.json` (`$comment`), both example profiles, `docs/stages/G-evaluation.md`, `docs/execution.md`, `tests/eval_fixtures.py`, `test_eval_{cli,concurrency,judge,config}.py`, new `test_eval_storage.py`, `test_field_contracts.py`.

**Fix round 1 commands and real output** (each through `tools/agent-workflow/test_slot.py`, one pytest process at a time, `--basetemp .tmp/T-016d/pytest`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH` = worktree `src` and `tests`; final sweep after the last edit, source untouched since):

| file | result |
|---|---|
| `test_eval_cli` (two-process test included) | 8 passed in 86.0 s |
| `test_eval_judge`, `test_eval_concurrency`, `test_eval_properties`, `test_eval_config`, `test_eval_storage` | 33, 11, 8, 211, 4 passed |
| `test_cli_commands`, `test_cli_config`, `test_cli_properties`, `test_property_settings` | 12, 70, 10, 1 passed |
| `test_storage_anchored_writes`, `_anchored`, `_tables`, `_config`, `_properties`, `_concurrency`, `_references` | 177, 27, 21, 10, 7, 4, 5 passed (5, 2, 0, 0, 0, 1, 0 skipped for OS reasons, as before) |
| `test_k0_scan`, `test_loader`, `test_intake_config`, `test_validation_config`, `test_enhance_serve_config`, `test_pipeline_config` | 16, 134, 31, 169, 197, 62 passed |
| `test_field_contracts` | 532 passed, 34 skipped in 192 s |
| `test_real_contracts`, `test_model_schemas` | 3, 14 passed |

Mutants F1 to F7 were each applied to the source, run against the named tests, killed and reverted (grep for `MUTANT` in `src` and `tests` is empty): F1 lock not taken, F2 lock not released after an exception, F3 repeated-key check removed (R8), F4 `LifecycleFull` counted completed, F5 import cap back to a constant, F6 refusal after `prepare` passes through (exit 2; killed by `ec::test_a_refusal_after_the_run_has_started_...`, re-run at handoff), F7 import takes no lock. The F2 mutant first survived the import path (garbage collection frees the lock) and was killed after the `SpiedLock` test was added.

Not run: lint and `ruff format --check` (ruff not installed); `tools/agent-workflow/tests/`; the full suite; Linux CI; a live run.

## not_run

A live run on the real draft set with real models (T-017); both-OS CI (PM; this was Windows only, 10 storage tests skip on OS grounds as before); lint and `ruff format --check` (`ruff` not installed); `tools/agent-workflow/tests/` (no tooling change); the full test suite (another session holds the machine: one file at a time); the ID-level link check, semantic metrics without scores, every confidence interval (the contracts are not approved); pre-mortem rows 2, 11, 13 for `--scores` and `--source-images` (gaps above).

## PM integration (2026-11-19)

A fresh Opus retest (`t016p4-rt-opus`) of fix round 1 failed only on three new small items; the PM fixed them at integration (decision 164):
- N1 flaky two-process test: `tests/eval_fixtures.py` gives the trace queue test-only headroom, `queue_limit = max(queue_limit, concurrency + 16)`, because a trace slot frees only after its write finishes. Runtime behaviour is unchanged: trace capacity is still a `not_run` unit (`trace_capacity`).
- N2 retest mutant O1 survived (the lock released before the final summary and pack): new test `ec::test_the_run_lock_is_held_while_the_final_summary_and_the_pack_are_written` records the lock state inside `refresh_summary` and `write_pack`. Mutant O1 re-applied by the PM: the test fails (killed); reverted.
- N3 unused `LOCK_NAME` and `DISPOSITIONS` removed from `eval_run.py` (the lock file name is the artifact-bound literal of `DataStore.run_lock`).

Evidence: `test_eval_cli.py` 5 runs, 9 passed each; the other eval test files green (11, 33, 4, 211, 8 passed). Decision 171: a Gemini Flash diff-only review of N1 to N3 before the patch lands.

## Noticed, not done

- Pre-mortem gaps 2, 11 and 13 above have no dedicated test.
- `BATCH_ROWS` is a safety-bound literal; promote it to an `evaluation.run.*` leaf if the user wants it tuned (`MAX_SIDECAR_BYTES` is now `evaluation.run.max_sidecar_bytes`).
- Single-output blinding limit (P3(c)) is documented, not removed: PM decision, accepted by the user (decision 170).
- Ctrl+C is not simulated in a test; it takes the same `finally` as an exception.
- `eval import` re-reads and re-summarises the run directory from its manifest; a run directory deleted by hand loses its imports, not its results.
- `tests/test_probe_monitoring_tmp.py` leftover noted by P3 is not mine.
- Scratch `.tmp/T-016d/` and the pytest basetemp are removed at handoff; the screens above are the evidence.
- PM integration (2026-11-19): `read_source_file` (pinned in the `k0` and `k1_image` stage inventories) now delegates to `iter_source_file`, and `tests/test_stage_revisions.py` was not in this task's test sets; the PM pinned `iter_source_file` and bumped `k0-scan-v6` and `k1-image-4` (Gemini review PASS).
