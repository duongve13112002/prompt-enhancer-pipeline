# T-016 Step 0 history (archived verbatim)

Moved from `tasks/active/T-016-cli-run-guide.md` when the phase 1 Completion report replaced the working Step 0 draft. The text below is unchanged: the scout draft (Gemini 3.8 Flash, 2026-11-18) and the PM review of it. The implementer's own Step 0 and the PM answers (decisions 154 and 156) stay in the task file.

## Step 0 draft (scout; PM review below)

### Unknowns and failure modes

- `enhance serve` needs an HTTP server; no web framework is pinned. The standard library (`asyncio` streams or `http.server.ThreadingHTTPServer`, as the T-015 chain test's double uses) avoids a new dependency but needs careful lifecycle code around `Pipeline` (`pipeline.py:77-139`).
- Shutdown must leave nothing running: `pipeline.py:123-136` and `response_lifecycle.py:113-144` drain traces, monitors and disk writes under timeouts; SIGINT or SIGTERM (and Windows console events) must reach that drain.
- `enhance once` returns an envelope plus `attachment_bytes` (`pipeline.py:28-36,182-189`); JSON on stdout cannot carry raw image bytes, so attachments need an output folder.
- `eval build`, `eval run`, `eval fit` have configuration reads (`config.py:82-88`) but no runner module.
- `serving.*` holds `max_concurrency`, `queue_limit`, `total_deadline_s`, `prefix_cache` (`configs/enhancer.example.yaml:403-408`) but no listen address or port.

### Command inventory (scout; served today per `__main__.py:15-40`)

| Command | Served | Calls | Output |
|---|---|---|---|
| `kb sync`, `kb audit` | yes | `kb_sync`, `kb_audit.audit_config` | JSON report |
| `kb status`, `kb rollback`, `kb gc`, `kb show` | no | knowledge store methods | JSON |
| `profile register`, `profile check` | no | `profiles.register_profile`, `profiles.check_profile` with `GoldenAdapter` (`pipeline.py:239`) | JSON |
| `enhance once` | no | `Pipeline.enhance` (`pipeline.py:137-236`) | envelope JSON, attachments to a folder |
| `enhance serve` | no | HTTP around `Pipeline` | HTTP service |
| `eval build`, `eval run`, `eval fit` | no | no runner yet | eval reports |
| embed server | yes (`prompt_enhancer.embed_server`) | | embedding HTTP |

### Ranked questions with proposed answers

1. **HTTP layer for `enhance serve`:** standard library only; `POST /v1/enhance` (the `request.v0` schema), `GET /health`, and an attachment download route; HTTP 503 on queue overflow.
2. **`enhance once` input and output:** `--request <file>` validated against `request.v0` (prompt-only flags optional); pure envelope JSON on stdout; attachments written under a configured or given output folder inside the project (AGENTS.md section 6).
3. **Concurrency and shutdown:** bounded by `serving.max_concurrency` and `serving.queue_limit`; on a stop signal, stop accepting, finish or cancel in-flight requests within the configured deadline, `await pipeline.close()`, exit with nothing left running.
4. **`eval *` scope:** see PM review.
5. **Run guide:** a new `docs/` guide: preflight (VRAM baseline), model servers, `kb sync`, `profile check`, `enhance serve`, smoke requests for generate, edit and retrieval off, teardown with VRAM back at baseline, and a result template the user pastes back.

### Design (scout)

- `__main__.py` extends its subparsers (`kb`, `profile`, `enhance`, `eval`); `--config` and `--overlay` go through `config.load_config(...)` with the T-015 version gate for request commands (`config.py:1808-1812,1959`).
- Exit codes: 0 success, 1 operational failure, 2 configuration or argument refusal (as `tests/test_embed_server_cli.py:94`).
- `enhance serve` runs inside `async with Pipeline(...)` (`pipeline.py:116-121`).

### Configuration impact (scout; existing keys)

`runtime.request_deadline_s`, `runtime.trace.*`, `runtime.monitoring.*` (T-015), `serving.max_concurrency`, `serving.queue_limit`, `serving.total_deadline_s`, `serving.prefix_cache`, `storage.kb_store`, `storage.data_root`, each already validated; T-016 adds only what the PM review below names.

### Plan (scout)

1. Dispatcher and argument validation (`kb status`, `rollback`, `gc`, `show`, `profile register`, `profile check`), with argument property tests.
2. `enhance once` end to end with the labelled HTTP double of `tests/test_pipeline_s0_s6.py` (generate, edit, retrieval on and off).
3. `enhance serve` with concurrency, overflow and shutdown tests (no process left).
4. `eval *` per the PM review.
5. The run guide.

## PM review (2026-11-18)

- **Listen address and port are profile values, not CLI defaults** (AGENTS.md section 5, R-015): add `serving.host` and `serving.port` as validated keys with the sentinel `deploy_required` in the base example and concrete dev values in the dev example; absent or sentinel refused at start. A CLI flag may override only if the design says so explicitly and tests both.
- **`eval run` is needed for the T-017 pilot**, which runs the T-003 scenarios (draft set in `data/eval/draft-2026-11-14/`) end to end and scores them. Proposed: T-016 implements `eval run` as "run every scenario of a named set through `Pipeline` and write one result record per scenario plus a summary", reusing the existing scenario format; scoring rules come from `docs/stages/G-evaluation.md` and are not invented; `eval build` and `eval fit` refuse with exit 2 and a precise message until their tasks (T-003 feature part, T-030). PM decision pending user review.
- Attachment output folder: a validated path under the project root; nothing written outside it.
- Windows and Linux: shutdown on Ctrl+C and on SIGTERM where it exists; tests run on both in CI (R-025).
- Builder: after T-015 acceptance, per decision 91 (T-016 is even: Claude builds, Codex reviews), subject to quota (S32).

## Phase 0 Completion report, Handoff and Worker completion (verbatim, moved from the task file at the phase 1 handoff)

- **Work completed (older, phase 0):** appended only this implementer Step 0; preserved the scout/PM text and pre-existing T-015 patch. No runtime/config/schema/draft-data edits.
- **Result and evidence:** read-only source/artifact checks above. Recall: `<venv-python> -B -I tools/agent-workflow/recall.py build --db .tmp/T-016/recall.sqlite` returned `indexed=94 unchanged=0 removed=0 refused=0 chunks=693 skipped_chunks=0`; searches `T-016`, `evaluation`, `"91"`, `"65"`, `prefix_cache` read before proposing questions. The initial combined query `T-016 eval scoring serving attachments` had no hits. Source/tool limitations: `rg` unavailable; preferred read/grep tools not exposed, and the discovered Node tool was rejected by the role allowlist. Continued with allowed scoped reads; no escalation or policy change attempted.
- **Important changes/reasons:** corrected command/exit-code/serving/shutdown claims, identified the lossy draft projection and missing scoring/provenance contracts, and separated proposals from accepted behavior. New controls and documentation scope changes require PM decisions rather than guessed implementation.
- **Configuration audit:** not applicable to phase 0, because no operational value changed; pre-code impact and planned evidence are above. Every test, lint/format, mutant, live/model/GPU/network run and both-OS execution is **not_run** here (plan-only authorization). Artifact inventory is a read-only source check, not quality/performance evidence.
- **Problems/remaining work:** Q1-Q5 and T-015 acceptance remain open. The whole documented command surface cannot honestly be called implemented while Q4 or scoring are unresolved.
- **Done-check evidence:** `git diff --check -- tasks/active/T-016-cli-run-guide.md` produced no output. `git diff --stat -- tasks/active/T-016-cli-run-guide.md` reported one changed file (the appended plan). Before/after `git diff --binary -- . ':(exclude).secrets' ':(exclude)tasks/active/T-016-cli-run-guide.md'` captures have identical SHA-256 `A3C089C884980DD7F15128E142D217DC7BF8C0B9AA417F4629548CE42775B156`, preserving the other tracked changes. Only this task file was edited outside disposable task scratch; no Git write was performed. Scratch source readers, inventory script, recall database and diff captures are removed before handoff.

### Handoff (phase 0)

- **Current status:** `STEP_0_AWAITING_PM_APPROVAL`. **Already done:** source verification, command inventory, dependency map, conditional design/configuration/test plan. **Next agent:** resolve questions and recheck accepted T-015 APIs, then implement only after the separate approval message. **Must not repeat:** do not reinterpret a draft field from prompt text, promise bounded disk preemption, count the existing skipped-knowledge test as attachment coverage, or make live calls from CPU tests.
- **Worktree/base:** `<project>/.tmp/worktrees/T-016`, main `8d6caf8` plus supplied T-015 patch; no partial runtime patch produced. **PM notes:** record accepted decisions in the PM-owned index/status; Q1-Q5 are still proposals. **Noticed, not done:** existing docs' global statements that nothing is implemented are stale (for example `docs/test-plan.md:3`); only command/run-guide reconciliation belongs in this task. **Lessons:** no new general policy proposed; apply the existing blocking-call, real-entry-point and anchored-write lessons.

### Worker completion (phase 0)

- [x] **Changes:** Appended the implementer's source-verified Step 0 and completion sections to this task file only; no runtime/configuration edits or Git writes. Preserved the supplied T-015 patch.
- [x] **Checks:** Source and read-only draft-artifact verification completed; `git diff --check -- tasks/active/T-016-cli-run-guide.md` passed with no output. Other tracked changes had matching before/after diff hashes, recorded above. Tests, lint/format, mutants and live runs are `not_run` because phase 0 authorizes a plan only.
- [x] **Cleanup:** Removed the owned `.tmp/T-016` readers, inventory script, recall database and diff captures; `Test-Path -LiteralPath .tmp/T-016` returned `False`. No service or model process was started.
- [x] **Remaining:** Await PM approval of Step 0 and Q1-Q5, plus T-015 acceptance. Implementation requires a separate approval message; none has started.

## Phase 1 (P1 and P2) Completion report, moved verbatim from the task file when P3 was reported (2026-11-19)

### Phase 1 (P1 configuration and dispatch, P2 `enhance once` and attachment publication)

Requirements: R-001, R-004, R-009, R-015, R-017, R-024, R-025, R-026, R-027. Evidence class of every model-facing test: `mock_contract` (labelled loopback HTTP doubles: `FullServerDouble`, `TwoViewSync`, `KnowledgePlanner`, `DraftingServer`); no live model, GPU, network or paid call was made.

#### Fix round 1 (QA feedback of `.tmp/T-016-rev/review-claude.md`, decision 164): F1 to F5

| Finding | Fix | Named test(s) |
|---|---|---|
| F1 (P2) an accepted cap up to `sys.maxsize` crashed `read(limit + 1)` with `MemoryError`/`OverflowError` | `cli_input.read_bounded(root, path, limit, label)` now reads through `k0_scan.read_source_file(..., max_bytes=limit)`: fixed 1 MiB chunks, an up-front size check, no allocation sized by the limit; the oversize case is the new `k0_scan.SourceTooLarge` (a `ScanError` subclass, so existing catches are unchanged). Every other read failure is a `Refusal` naming the argument; any other exception before the pipeline is `failed: <Type>`, exit 1 (`cli_enhance.run_once`) | `test_read_bounded_returns_the_exact_bytes_or_a_precise_refusal_for_any_validated_limit` (property, up to `sys.maxsize`), `test_read_bounded_allocates_nothing_for_a_large_limit_and_refuses_an_oversize_file[1,2**31,2**62,maxsize]`, `test_a_request_cap_of_sys_maxsize_reads_a_normal_request_and_still_delivers`, `test_an_unexpected_failure_before_the_pipeline_is_exit_one_with_the_type_only[RuntimeError,MemoryError,RecursionError]` |
| F2 (P3) the request path followed links inside the root; check-then-open gap | PM decision: refuse links. `resolve_input` accepts a path only when the resolved path equals the spelled one (the `storage/_fs.real_inside` rule); the file is then opened component by component without following a link (`read_source_file`: `O_NOFOLLOW` or the Windows reparse-point check), so no gap remains. The same rule covers `--config` and `--overlay` of every new command (`refuse_linked_configuration`) | `test_a_request_below_a_linked_directory_is_refused_before_anything_starts` (junction on Windows, symlink on POSIX), `test_the_reader_itself_never_follows_a_link_even_when_the_path_rule_is_bypassed`, `test_a_request_that_is_itself_a_symlink_is_refused` (POSIX only; skipped on Windows, where a file symlink needs a privilege), `test_every_new_command_refuses_a_linked_configuration_or_overlay` (14 cases) |
| F3 (P3) the `attachments/` listing was outside the expiry fault handling | `expire_attachments` counts an `OSError` from the listing as `failed` and returns; the run continues and prints `warning: expired attachment cleanup failed for N entr(y\|ies)` | `test_an_unreadable_attachments_listing_is_counted_and_never_stops_the_request` |
| F4 (P3) audit rows cited tests that did not cover `--overlay`, the trace-id guard or the expiry counts | new tests, and the audit rows below now cite them | overlay: `test_overlay_supplies_a_value_the_base_profile_leaves_unresolved` (7 commands), `test_a_bad_overlay_is_refused_by_every_new_command_before_any_side_effect` (21 cases); trace id: `test_the_trace_id_is_checked_before_a_path_is_built` (9 ids); expiry counts: `test_expiry_counts_each_outcome_exactly` and the added `'warning' not in stderr` assertion of `test_once_deletes_expired_attachment_copies_on_the_next_run` |
| F5 (P3) the dev value comment and a doc row said more than the code | `configs/enhancer.dev.example.yaml` now calls `23068672` a proxy (base64 of the 16 MiB `sync.retained_images.max_image_bytes` plus slack; `request.v0` has no length limit, so no request bound exists), the audit row says the same, and `docs/execution.md` `kb status` says a missing store is reported (`store_present: false`, exit 0). The PM answer to Q2 ("from the request schema's largest permitted source image") is not edited here; it is superseded by this note | `test_overlay_supplies_a_value_the_base_profile_leaves_unresolved[kb status]` (absent store: exit 0, `store_present` false), `test_kb_status_reports_stored_facts_and_does_not_check_the_live_source`, the dev-profile assertion of `test_cli_config.py` (value 23068672) |

Also fixed: a generated-argv flake of `test_a_refused_request_path_leaves_nothing_behind` (Hypothesis produced the path `-:`, which argparse reads as an option, a usage exit instead of a refusal); the test now passes `--request=<path>`.

#### Behaviour

- `python -m prompt_enhancer` now dispatches `kb sync|audit` (legacy, unchanged exit 1 for configuration and store errors), `kb status|show|rollback|gc`, `profile register|check`, `enhance once`, and answers `enhance serve`, `eval run|import|build|fit` with exit 2 and "not yet implemented in this build" (build/fit keep their documented task pointers). A new `--project-root` is validated and contains the configuration, overlay, request and set paths.
- Exit codes of the new commands: 0 completed or delivered (an honest failed or degraded envelope is still delivered, R-024), 1 operational failure (including a failing golden in `profile check`, a delivery or publication failure), 2 refusal made before any side effect (arguments, configuration, input, unserved commands).
- `enhance once --request <file>` order of effects: load and validate configuration (version gate, read set, `serving.http.max_request_bytes`) -> read the request without following links, in bounded chunks, as strict UTF-8/JSON (`model_json.strict_json`, duplicate keys and non-finite numbers refused, `request.v0` schema) -> expire old attachment copies -> `Pipeline.enhance` -> publish attachment copies when the response holds bytes -> write the envelope JSON to stdout -> `after_delivery()` only if the write succeeded. Prompt text and the admin system text are passed through untouched (R-001, R-004). Retrieval off never opens the knowledge base: the fixture runs with the KB absent (R-009). Knowledge-base text is never interpreted by the wrappers (R-026).
- Attachments: `data_root/attachments/<trace_id>/index.json` is written first, then each `<image-hash-hex>` copy; bytes must hash to their image id; a naive expiry or a duplicate trace directory is refused; a failure rolls back the copies. The expiry is `runtime.trace.retention_s` and is applied at the start of the next `enhance once` run. A publication failure prints `failed: delivery (<Type>)`, returns 1 and leaves the ticket unacknowledged (the lifecycle then records `delivery_unacknowledged`).
- `kb status` reports stored facts only (store present, all snapshot rows, the published snapshot's manifest hash and scan summary, row counts of `folders`, `concepts`, `images`, `cards`, `aliases`, folders with an error) and marks live source changes `not_checked`; `kb sync --only` is refused; `kb show` reads a published snapshot lease (no lint: none is stored in the snapshot tables); `kb rollback` moves the pointer only to a `retired` snapshot; `kb gc` runs under the writer lock with `sync.gc.retention` and `sync.snapshots.keep`; `profile register` uses the production bound `manifest_drafter`; `profile check` runs request-command preflight, then the production `Pipeline` with `GoldenAdapter` and never auto-confirms.

#### P1/P2 checkable finish -> named tests

| Finish (Step 0 table) | Test |
|---|---|
| Every documented command has a real entry-point case or a precise refusal | `test_cli_commands.py`: status, show, rollback+gc, missing store, unserved commands, legacy exit 1 vs new exit 2, register/check, failing check exit 1 |
| Profile check uses GoldenAdapter with full preflight | `test_cli_commands.py` profile check tests; `test_cli_config.py::test_every_documented_read_of_a_command_is_refused_when_unresolved[profile check:*]` |
| Argument/path/configuration tests before any side effect | `test_cli_config.py` (70) leaf matrix asserts exit 2, empty stdout, tmp tree unchanged; `test_cli_enhance.py::test_once_refuses_a_request_path_outside_the_root_or_not_a_file`, refusal matrix of 10 request cases |
| Existing sync/audit compatibility | `test_kb_sync_gates`, `test_k0_sync`, `test_kb_audit`, `test_kb_sync_diagnostics` (all green) |
| Generate and edit x retrieval on/off through `python -m prompt_enhancer` with labelled doubles | `test_cli_enhance.py`: retrieval-off generate and edit with the KB absent; retrieval-on generate; envelope checked with `validate_envelope` |
| Prompt and system text preserved | `test_cli_enhance.py` prompt/system preservation test |
| Real attached references, hash-equal bytes | `test_cli_enhance.py` retained-attachments fixture (edit, retrieval on, bytes equal under `data_root/attachments/<trace_id>/`); `test_publication_writes_exact_bytes_refuses_a_mismatch_and_never_merges_a_trace` |
| Expiry | `test_once_deletes_expired_attachment_copies_on_the_next_run` |
| No acknowledgement without delivery | `test_once_acknowledges_only_after_the_envelope_was_written`, `test_once_does_not_acknowledge_when_stdout_cannot_be_written`, publication-failure test |
| Invalid paths/JSON refused | refusal matrix; `test_cli_properties.py` (9 Hypothesis properties: argv, UTF-8/JSON bytes, truncation, duplicate keys, containment) |

#### Commands run and results

Each file in its own process through the slot, fixed `--basetemp .tmp/T-016/pytest`, `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<root>/src:<root>/tests`, venv python, `-B -X faulthandler -m pytest -q -p no:cacheprovider`:

- Fix round 1, final runs after the last source and test edit (each its own process): new `test_cli_inputs` 64 passed 1 skipped (the POSIX file-symlink case); `test_cli_properties` 10 passed (twice; the read_bounded property is the new one); `test_cli_config` 70 passed; `test_cli_commands` 15 passed; `test_cli_enhance` 24 passed (97 s); `test_pipeline_s0_s6` 14 passed; files that import `k0_scan` (changed: new `SourceTooLarge`): `test_k0_scan` 16, `test_k0_checkpoint` 16, `test_k0_review_fix` 42, `test_k0_sync` 24, `test_k1_config` 558, `test_kb_audit` 12 passed; scanners `test_storage_anchored_writes` 172 passed 5 skipped, `test_property_settings` 1 passed; `test_loader` 134, `test_pipeline_config` 62 passed (dev profile comment edited). `test_field_contracts` was not rerun: `config.py` and the schema did not change in this round (last full run in round 0: 515 passed, 34 skipped). One earlier run of `test_cli_properties` failed on the generated path `-:` (test flake, fixed above) before the final passes.
- Round 0 results: `test_cli_enhance` 24 passed (92 s), `test_cli_commands` 15 passed, `test_cli_config` 70 passed, `test_cli_properties` 9 passed.
- Scanners: `test_storage_anchored_writes` 172 passed 5 skipped; `test_property_settings` 1 passed; `test_field_contracts` 515 passed 34 skipped (rerun after the last `config.py` edit).
- Regression, 37 existing files, all passed: embed_server_cli 8; config 160; dev_compare 71; lifecycle 71; read_set 92; reference 149; fix01 60 (1 skipped); fix02 221; fix03 13; k0_config 33; k0_review_fix 42; k0_sync 24 (rerun, 24); k1_config 558; k2_name_override 47; k2_observe_config 193; k4_config 273; k5_config 52; kb_audit 12; kb_sync_diagnostics 10; kb_sync_gates 19; kb_sync_production_probes 1; loader 134 (rerun, 134); model_client_image_side 36; model_composition 54; model_config 123; model_production_probes 84; pipeline_s0_s6 14; pipeline_config 62 (rerun, 62); probe_request_settings 132; real_contracts 3; retained_images 63 (2 skipped); storage_config 10; storage_review_r1 42; planner 67; intake 71; profiles 177 (2 skipped).
- Class sweep: `tests/` grepped for every changed string, id and digest (the new `serving.http` block in the examples and the schema, the `FullServerDouble` hooks, `__main__` names `load_config`/`sync_config`/`audit_config`); the files that touch them are in the list above.
- `not_run`: lint and format (`ruff` is not installed in `.venv`); CI on Windows and Linux (PM-owned, R-025; local Windows success does not show Linux); any live, GPU or deployment run; P3 to P5 tests.

#### Mutants (scratch copies of `src` under `.tmp/T-016/mut`, checkout untouched)

| # | Mutant | Result |
|---|---|---|
| M1 | request byte cap off by one (`>` to `>=`) | killed: `test_once_limit_override_changes_what_is_refused` |
| M2 | request path containment removed | killed: `test_once_refuses_a_request_path_outside_the_root_or_not_a_file[outside_root]` |
| M3 | attachment publication skipped | killed: `test_once_acknowledges_only_after_the_envelope_was_written` |
| M4 | acknowledge without delivery | killed: `test_once_does_not_acknowledge_when_stdout_cannot_be_written` |
| M5 | leaf `_sentinel` in `validate_request_limit` removed | survived, equivalent: the block-level `_sentinel` already names the leaf; the redundant line was removed from the code and the sentinel cases now expect "unresolved sentinel ..." |
| M6 | expiry comparison inverted | killed: `test_once_deletes_expired_attachment_copies_on_the_next_run` |
| M7 | attachment hash check removed | killed: `test_publication_writes_exact_bytes_refuses_a_mismatch_and_never_merges_a_trace` |
| M8 | refusal exit 2 -> 1 | killed: `test_kb_show_refuses_when_nothing_is_published` |
| M9 | rollback to a non-retired snapshot allowed | killed: `test_kb_rollback_moves_the_pointer_only_to_a_retired_snapshot_and_gc_runs` |
| M10 | `serving.http` shape/unknown-key check removed | killed: `test_limit_refuses_a_missing_or_misshaped_block[extra_key]` |

Fix round 1 mutants (the reviewer's surviving ME and MG, and mutants of the new code; the same runner, one at a time):

| # | Mutant | Result |
|---|---|---|
| ME | expiry removes an unindexed directory unconditionally (`if not os.listdir(directory)` -> `if True`) | killed: `test_expiry_counts_each_outcome_exactly` (counts `(0, 1)` instead of `(0, 0)`) |
| MG | `check_request_id` removed from `attachment_path` | killed: `test_the_trace_id_is_checked_before_a_path_is_built[]` |
| MH | the cap is not passed to the reader (`max_bytes=None`) | killed: the `read_bounded` property |
| MI | an oversize file reported as a generic read failure (`except SourceTooLarge` disabled) | killed: `test_read_bounded_allocates_nothing_for_a_large_limit_and_refuses_an_oversize_file[1]` |
| MJ | spelled-versus-resolved path rule removed from `resolve_input` | killed: `test_a_request_below_a_linked_directory_is_refused_before_anything_starts` |
| MK | linked-reader failure reported as a generic one (`except LinkRefused` disabled) | killed: `test_the_reader_itself_never_follows_a_link_even_when_the_path_rule_is_bypassed` |
| ML | overlay path rule removed from `refuse_linked_configuration` | killed: `test_every_new_command_refuses_a_linked_configuration_or_overlay[overlay-enhance once]` |
| MM | an unreadable attachments listing is fatal (`return removed, failed + 1` -> `raise`) | killed: `test_an_unreadable_attachments_listing_is_counted_and_never_stops_the_request` |
| MN | the generic failure handler before the pipeline narrowed to `KeyError` | killed: `test_an_unexpected_failure_before_the_pipeline_is_exit_one_with_the_type_only[failure0]` |

The retrieval-off branch of the pipeline is production code from T-015 and was not changed here; its refusal and KB-absent behaviour is covered by the retrieval-off fixtures, not by a mutant.

#### Configuration audit (row by row)

Every row: validated source, example profile, non-default override test, refusal test, class. Test names are in `tests/test_cli_config.py` unless stated.

| Value or behaviour | Config path | Example | Override test | Refusal test | Class |
|---|---|---|---|---|---|
| Project root, config, overlay, request path | CLI `--project-root`, `--config`, `--overlay`, `--request` (no-link rule, F2) | n/a (arguments) | `test_cli_enhance` runs with a tmp root; `test_cli_inputs.py::test_overlay_supplies_a_value_the_base_profile_leaves_unresolved[each of the 7 commands]` (base key `deploy_required`: refused alone, accepted with the overlay) | `test_once_refuses_a_request_path_outside_the_root_or_not_a_file`; `test_a_bad_overlay_is_refused_by_every_new_command_before_any_side_effect` (3 flaws x 7 commands); `test_every_new_command_refuses_a_linked_configuration_or_overlay` (2 x 7); `test_a_request_below_a_linked_directory_is_refused_before_anything_starts`; `test_the_reader_itself_never_follows_a_link_even_when_the_path_rule_is_bypassed`; `test_a_request_that_is_itself_a_symlink_is_refused` (POSIX only, skipped on Windows) | configurable; flag names protocol-bound |
| Request byte cap | new `serving.http.max_request_bytes` (schema `enhancer_config.v0`, required block, exact positive integer, `0 < v <= sys.maxsize`) | base and `deploy-vllm` overlay `deploy_required`; dev `23068672` (a dev-only proxy, not measured: base64 of the 16 MiB `sync.retained_images.max_image_bytes` plus slack; `request.v0` has no length limit, so no request bound exists to derive it from) | `test_limit_accepts_a_positive_integer_and_returns_it[1,4096,23068672,maxsize]`; `test_once_limit_override_changes_what_is_refused`; `test_a_request_cap_of_sys_maxsize_reads_a_normal_request_and_still_delivers` (CLI end to end); property `test_read_bounded_returns_the_exact_bytes_or_a_precise_refusal_for_any_validated_limit` (limits up to `sys.maxsize`); `test_read_bounded_allocates_nothing_for_a_large_limit_and_refuses_an_oversize_file[1,2**31,2**62,maxsize]` | `test_limit_refuses_an_unresolved_or_malformed_value_and_names_the_key` (12 values), `test_limit_refuses_a_missing_or_misshaped_block` (5 shapes) | configurable |
| Limit is not part of the library read set | `COMMAND_READS['enhance once']` has no `serving` key | n/a | `test_the_limit_is_read_by_the_command_line_not_by_the_library_read_set` | same test (unresolved limit refused by the CLI only) | deviation D1 |
| Shipped profile values | the three example profiles | listed above | `test_the_shipped_profiles_leave_the_limit_to_the_deployment_except_the_dev_profile` | covered by loader and field-contract suites | configurable |
| `kb status`, `kb show` | `storage.kb_store`, `storage.lock` | existing | `test_cli_commands` status/show against a tmp store | matrix `kb status:*`, `kb show:*` (sentinel -> exit 2, no output, tree unchanged) | configurable |
| `kb rollback` | `storage.kb_store`, `storage.lock`, `storage.table_format` | existing | rollback test | matrix `kb rollback:*` | configurable; table format artifact-bound |
| `kb gc` | the three above plus `sync.gc.retention`, `sync.snapshots.keep` | existing | gc test with `keep=3` | matrix `kb gc:*` | configurable |
| `profile register` | `profiles.root`, `profiles.exclude`, `creativity` (every leaf), `vlm.roles.manifest_drafter`, `storage.lock` | existing | register test | matrix `profile register:*` | configurable |
| `profile check` | `profiles.root`, `profiles.exclude`, `storage.lock`, plus the `enhance once` preflight reads before the golden pipeline | existing | check tests (pass and failing golden) | matrix `profile check:*` | configurable |
| `enhance once` | `profiles`, `rag`, `vision`, `candidates`, `embedding`, `planner`, `resolution`, `clarification`, `creativity`, `edit`, `assembly`, `writer`, `validation` (leaf set `VALIDATION_READS`), `runtime.request_deadline_s`, `runtime.trace.*`, `storage.data_root`, `storage.table_format`, `vlm.roles.planner|writer` (`verifier` is read at S5/S6, T-014) | existing | retrieval-on and retrieval-off fixtures | matrix `enhance once:*` run with retrieval on (every leaf `deploy_required` -> exit 2 before any side effect) | configurable |
| Retrieval-off trimming | `rag.enabled: false` drops `candidates`, `embedding`, `assembly` | n/a | retrieval-off generate and edit with the KB absent | unresolved retrieval-only leaf does not stop an off run (`_command_reads`, existing `test_loader`) | configurable |
| Key outside a command's read set | e.g. `vlm.roles.planner` for `kb status` | n/a | `test_a_key_outside_a_commands_read_set_does_not_stop_it` | n/a | configurable |
| Attachment root | `storage.data_root` + `attachments` | `data` | `test_cli_enhance` real retained attachments (tmp data root) | publication unit test (duplicate trace, hash mismatch, naive expiry); `test_cli_inputs.py::test_the_trace_id_is_checked_before_a_path_is_built` (9 bad trace ids, the guard of `attachment_path`) | root configurable; `attachments`, `index.json`, `<image-hash-hex>` artifact-bound |
| Attachment expiry | `runtime.trace.retention_s` | existing sentinel in base/dev | `test_once_deletes_expired_attachment_copies_on_the_next_run`; `test_expiry_counts_each_outcome_exactly` (removed/failed counts for live, stray, empty, unreadable-index, expired sets) | matrix key `runtime.trace`; `test_an_unreadable_attachments_listing_is_counted_and_never_stops_the_request` | configurable |
| Exit codes 0/1/2 | none | n/a | exit-status assertions in all CLI files | `test_cli_commands` legacy 1 vs new 2 | protocol-bound |
| Retained literals | `REFUSED = 2`, `FAILED = 1`, stderr prefixes `refused: ` / `failed: `, `STATUS_TABLES`, `OPERATIONAL_PROFILE_CODES`, `ATTACHMENT_DIR`, `INDEX_NAME` | n/a | exact stderr prefix and exit status asserted in the matrix | n/a | exit codes and prefixes protocol-bound; `STATUS_TABLES` and attachment names artifact-bound; `OPERATIONAL_PROFILE_CODES` protocol-bound (codes of the profile module); no threshold, model, device or timeout was added |

No model, provider, endpoint, device, worker count or timeout value is introduced by these commands: all are read from the profile by the existing validators. `serving.host`, `serving.port` and the other `serving.http.*` keys are P3 and are not added.

#### Deviations, decisions and noticed, not done

- **D1** The request byte cap is read by `validate_request_limit` in the CLI, not through `COMMAND_READS`, so library callers and golden checks need no value. Only `serving.http.max_request_bytes` was added; the other `serving.http.*` keys, `serving.host` and `serving.port` belong to P3.
- **D2** `kb_audit._audit_snapshot` is reused for `kb show`; `kb show` has no lint because none is stored.
- **D3** `enhance once` with no active profile returns an honest failed envelope with exit 0 (a delivered failure, R-024).
- **D4** `PROMPT_ENHANCER_TEST_SRC` is a test-only knob in `tests/cli_fixtures.py` (lets mutants run the child process from a scratch copy of `src`); `FullServerDouble` gained the hooks `planner_answer` and `scripted_answer` (behaviour unchanged).
- **D5** A failed duplicate publication may remove an empty directory of the same trace id (benign).
- **D6** (fix round 1) The no-link rule applies to the three paths the new commands take from the command line (`--config`, `--overlay`, `--request`). The configuration files those name through `extends` and `overlay_of`, the profile bundle and the schema files are read by the shared loader (`config._inside`), which still follows links that stay inside the root; changing it would alter `kb sync`/`kb audit` and every loader test, so it is left to the PM (see Noticed, not done). A differently cased or 8.3-short spelling of the same path is refused as "another spelling", as the stores do.
- **D7** (fix round 1) `k0_scan` gained the subclass `SourceTooLarge(ScanError)`, raised in `read_source_file` where `ScanError` was raised for an oversize file; message and base class are unchanged.
- **Noticed, not done:** `tests/test_probe_tmp.py` is an emptied leftover probe, to be deleted by the PM; docs outside `docs/execution.md` still say nothing is implemented (for example `docs/test-plan.md:3`); a hard process-exit deadline for stalled OS writes is outside T-016 (Q5); the shared loader's link-following for `extends`, `overlay_of`, profile and schema paths (D6); the review's note that `cli_commands` imports the private `kb_audit._audit_snapshot` (D2): a public helper is advisable before P3 reuses it; the Windows file-symlink variant of the request link test is not exercised (needs a privilege), only the junction and the POSIX symlink variants.
- **Lessons learned:** the K5 serving-index fingerprint needs the request config to copy `index.*` and `embedding.query_text_limit` from the sync config; a planner mention must occur in the prompt; without `monitoring_selected` the trace row is written at submit, so acknowledgement is observed with a spy on `PipelineResponse.after_delivery`; `src` modules must pass the anchored-write scanner.

#### Changed files

New: `src/prompt_enhancer/{attachments,cli_input,cli_enhance,cli_commands}.py`, `tests/{cli_fixtures,test_cli_commands,test_cli_config,test_cli_enhance,test_cli_inputs,test_cli_properties}.py`, `tasks/archive/T-016-step0-history.md`. Modified: `src/prompt_enhancer/{__main__,config,k0_scan}.py`, `contracts/schemas/enhancer_config.v0.schema.json`, `configs/enhancer.example.yaml`, `configs/enhancer.dev.example.yaml`, `configs/overlays/deploy-vllm.example.yaml`, `docs/execution.md`, `tests/test_pipeline_s0_s6.py`, this task file. Leftover: `tests/test_probe_tmp.py` (delete).

## Handoff (phase 1), moved verbatim from the live task file

- **PM integration (2026-11-19):** the diff-only retest passed (F1 to F5 fixed, D6 accepted as a recorded follow-up). The PM fixed the review notes N1 (`docs/execution.md` now calls the dev value a proxy) and N3 (`run_once` maps Ctrl-C before the pipeline to exit 1 `interrupted`, test `test_an_interrupt_before_the_pipeline_is_exit_one_without_a_traceback`); N2 (no test of a file that grows during the bounded read; mutant ML equivalent for static files) stays a follow-up. `tests/test_probe_tmp.py` was not integrated. P3 to P5 start with a fresh builder on the integrated base.
- **Status:** `P1_P2_FIX_ROUND_1_READY_FOR_REVIEW` (F1 to F5 fixed, each with named tests above; `tests/test_probe_tmp.py` left for the PM, as instructed; lint and `ruff format --check` `not_run`: `ruff` is not installed). P3 (`enhance serve`), P4 (`eval run`/`import`, `eval build`/`fit` refusals already in place) and P5 (run guide) are not started; they stay refused with exit 2 and a precise "not yet implemented in this build" message.
- **PM actions:** (1) delete the leftover `tests/test_probe_tmp.py` (the worker cannot delete a tracked-area file; it holds one comment line); (2) both-OS CI; (3) move the "Noticed, not done" items and deviations below into `docs/implementation-notes.md`.
- **Worktree/base:** `<project>/.tmp/worktrees/T-016b`, main `882cf19`. No Git write was performed. Scratch under `.tmp/T-016/` and the basetemp are removed.

## Step 0 sections moved verbatim from the live task file (phase 0, closed; P3 fix round)

### Unknowns, failure modes and corrections to the scout

All code claims in this section are **source-verified**; document requirements are **documented**; designs and unresolved choices are **proposals**, not measurements.

1. **The command inventory is incomplete, even for an existing command.** `__main__.py:15-46` serves only `kb sync` and `kb audit`. The sync path is `k0_sync.sync_config` -> `asyncio.run(kb_sync.sync_knowledge)` (`k0_sync.py:119-124`), not a direct CLI call to a `kb_sync` function. `docs/execution.md:18` also promises `--only <folder>`, but neither parser nor coordinator accepts it (`kb_sync.py:90`). Subsetting a full-snapshot sync risks treating excluded folders as deleted. `kb status` promises pending changes although its documented inputs and `COMMAND_READS` only include store/lock values (`docs/execution.md:17`; `config.py:58`). Existing store readers expose snapshots/current, not a pending-source-change or aggregate status API (`storage/store.py:447-452`, `:485-489`). These need a scope decision, not fabricated counts.
2. **The scout overstates existing serving validation.** The schema accepts zero concurrency and zero deadline and has no host/port (`contracts/schemas/enhancer_config.v0.schema.json:5206-5293`); `enhancer_config.v1` inherits the serving section through the loader's local-reference expansion (`config.py:1748-1767`). Consumed sentinels are refused, but useful positive bounds/cross-field checks must be added. A scoped search of `src/prompt_enhancer/*.py` found no `prefix_cache` reader; the setting currently describes external-server configuration (`configs/enhancer.example.yaml:407`; `docs/serving.md:37`). It must receive an explicit owner rather than being silently read and ignored. No web framework is declared (`pyproject.toml:8`). The existing embedding server is a useful bounded-transport reference, not a drop-in pipeline server (`embed_server/server.py:10-21`, `:452`, `:811`).
3. **Delivery and shutdown are separate from generation.** `PipelineResponse.after_delivery()` must follow successful delivery (`pipeline.py:28-35`); the pipeline does not call it for ordinary callers. `Pipeline.close()` cancels maintenance, closes the lifecycle and synchronously expires traces (`pipeline.py:123-135`). Disk writes that exceed `trace.drain_timeout_s` are recorded and then awaited; executor shutdown joins them (`response_lifecycle.py:137-144`). Thus T-015 does not promise a hard wall-clock shutdown bound. Admission, queued requests, slow HTTP readers/writers, console output and thread joins require their own blocking-call inventory. Cancelling an await does not establish that an owned thread exited.
4. **Attachments are not paths or URLs in the response contract.** They are metadata plus separate bytes (`envelope.py:35-49`; `pipeline.py:62-74`, `:236`). Metadata cannot simply gain an output-path field. T-015 holds the pinned snapshot for attachment retention but does not persist a CLI output directory or expose a download service. Trace expiry releases holds (`trace_store.py:33-35`, `:48-71`). Windows filenames cannot use the raw `sha256:` image ID. Copy publication, download expiry, delivery failure and path/link containment need tests through anchored storage (`storage/_anchored.py:60-94`), including the scanner guard learned in `tasks/LESSONS_LEARNED.md:78`.
5. **The T-003 draft is not an executable request set or a complete scoring contract.** It was accepted as a silver sample, built before a snapshot (`tasks/active/T-003-evaluation-set.md:16`, `:44-60`; decision 65 at `tasks/archive/decisions-054-066.md:18`). The shared project's read-only `data/eval/draft-2026-11-14/` exists; this worktree has no local copy. `scenarios.v0.jsonl:1` contains concept/constraint keys; `:36` identifies an edit source by image/folder IDs, not base64; `:51` has no retrieval-switch field despite being a `rag_off` scenario. `request.v0` instead accepts base64 source bytes and typed overrides (`contracts/schemas/request.v0.schema.json:7-13`). The loss of family, edit and retrieval information in v0 is already documented (`T-003-evaluation-set.md:59`). Do not infer a retrieval switch from prompt text or use the lossy JSONL projection as the pilot's sole input.
6. **Scoring requires decisions that the scout did not identify.** G requires independent semantic scoring, concept-separated splits, denominators and confidence intervals (`docs/stages/G-evaluation.md:9-14`); it gives no complete per-metric matching algorithm, interval method/confidence level, independent scorer binding or pilot snapshot-binding format. `eval_results` has a JSON `metrics` column but no dedicated snapshot/trace columns (`storage/tables.py:124-135`), and `DataStore.append` alone is not a resumable runner (`storage/store.py:1358-1377`). Treating the verifier under test as its own evaluator, scoring prose with word lists, fitting on dev results or calling an unconfigured paid evaluator would violate the requirements.

Read-only artifact inventory (not a model/test run): 55 records, 38 generate and 17 edit; 11 kinds with five records each. Parquet has the ten v0 columns plus `draft_link_details`, `draft_edit_key`, `draft_rag_enabled`, `draft_language`, `draft_format`; its schema metadata is `None`. All 55 rows have retrieval flags and link details; 17 have edit keys. These facts came from reading JSONL and Parquet with the project environment, not from assuming the scout's format. Evidence identities: `scenarios.v0.jsonl` SHA-256 `7d50442cb80c348f3388ccd4838ef17fc860ae9aebde65cf492bc8497b8a02ca`; `scenarios.manual.json` `3a18ff727ff8abb0b11786248f7dd573ee052b09887556bbf2583c5031a98eed`; `scenarios.parquet` `cf7811d17641b3bf0797a7fa7c9fe46f102da6e158fd0135a569d9ecb47b01c1`. No draft or source image was modified or rendered.

### Re-verified command inventory

Paths in this table are under `src/prompt_enhancer/` unless stated otherwise. The requested command surface is `docs/execution.md:15-29`.

| Command | Current entry point and callable evidence | T-016 work/output |
|---|---|---|
| `kb sync [--plan]` | Wired, including `--force`; `__main__.py:20-24`, `:34-43`; `k0_sync.py:119-124` | Preserve JSON, disabled-retrieval skip and guard refusal. `--only` is missing; Q4. |
| `kb audit` | Wired; required folder/card counts and seed; `__main__.py:25-38`; `kb_audit.py:57-89` | Preserve sampled human-audit JSON; this is not automatic fact-precision scoring. |
| `kb status` | Unwired; `KnowledgeStore.current/snapshots`, `storage/store.py:447-452`, `:485-489` | JSON of current/retained snapshots and available stored diagnostics; pending-source semantics need Q4. |
| `kb rollback <snapshot>` | Unwired; writer operation `storage/store.py:906-916` | Own writer context, retained-target validation and JSON result; no private pointer rewrite. |
| `kb gc` | Unwired; writer operation `storage/store.py:969-995` | JSON GC report respecting retained snapshots, leases and holds. |
| `kb show <concept>` | Unwired; pinned `read_rows`, `storage/store.py:656-662`; table shapes `storage/tables.py:76-82` | Card, aliases, folder bindings and available lint from one pin; precise unknown-concept refusal. |
| `profile register` | Unwired; async `register_profile`, `profiles.py:273-341`; production client `model_client.py:251` | Draft-only manifest/report with an explicitly named profile; no inferred admin confirmation. |
| `profile check` | Unwired; async `check_profile`, `profiles.py:575-658`; `GoldenAdapter`, `pipeline.py:239-252` | Confirmed-manifest/conflict/golden checks, activation only on success. Guide covers manual confirmation (`profiles.py:344-349`); no extra confirm command required. |
| `enhance once` | Unwired; async `Pipeline.enhance`, `pipeline.py:137-236` | Strict request file -> unchanged envelope JSON on stdout; attachment files outside payload; Q3. |
| `enhance serve` | Unwired; no pipeline HTTP transport | Health/enhance/attachment routes, bounded admission, explicit overload and owned shutdown; Q2/Q3/Q5. |
| `eval run` | Unwired; read set exists (`config.py:84-87`), data tables exist (`storage/tables.py:124-130`); no evaluation runner in the package | Named scenario set through production pipeline, durable result per scenario and summary, scoring subject to Q1. |
| `eval build`, `eval fit` | Unwired; read sets only (`config.py:82-88`) | PM proposal: recognized commands refuse with exit 2 and name their owning tasks (T-003 feature, T-030), before loading models or doing writes. Approval pending. |
| Embedding commands | Separate `python -m prompt_enhancer.embed_server`; `embed_server/__main__.py:43-93`, `:126`, `:152`, `:181-193` | Preserve separate CLI (serve/reference-check/dev-compare); guide links it. Not an existing `kb/profile/enhance/eval` subcommand. |
