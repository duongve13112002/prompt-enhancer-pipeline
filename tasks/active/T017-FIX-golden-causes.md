# T017-FIX: addendum layout, generate planner schema and lost failure reasons (profile check J4a2)

Kind: code. R-IDs: R-001, R-004, R-009, R-015, R-026. Role: implementer (Claude). Base: worktree `.tmp/worktrees/T017-FIX`.
Source report: `.tmp/T017-FIX/rca2-report.md` (copy of `.tmp/T017-RCA2/report.md`), read whole.
Status: implemented; review fix round 1 done (section "Fix round 1" before the Completion report). Step 0 below is kept as approved; where the PM answers or later findings changed it, the change is marked "Superseded".

### PM answers (phase 2)

1. `model-call-3` with its own pin row: yes. 2. Refuse both keys on cloud and on chat-less `openai_compatible` backends, refuse the separator with the separate layout, each with a refusal test whose message names key, backend and reason. 3. Follow `contracts/README.md`. 4. `run_case` approved; a real `GoldenAdapter` must write non-null diagnostics. 5. Static generate schema approved; the edit path must still send `planner_reply.v0` and accept a non-empty `preserve`. Plus an R-026 marker test.

**Versioning rule followed:** `contracts/README.md:280` "JSON Schema draft 2020-12 files in schemas/, versioned in the name; a change is a new version." New versions: `plan_result.v1` (`error.cause`), `writer_result.v1` (`effective_system_hash`), and `validation_result.v1` (its `$ref` moves to writer v1). Readers move to v1; the v0 files stay as superseded files (README practice, :109). `enhancer_config.v0` is edited in place, as every earlier key addition was (T-014, 04afa56; K3-RETRY now); see Noticed. `eval_result_metrics.v0` is unchanged.

### Configuration impact

| Setting | Schema | Profile source | Read by | Kind |
|---|---|---|---|---|
| `vlm.backends.<name>.addendum_layout` (`separate_system_message`, `joined_system_message`) | `enhancer_config.v0` backend properties; rules in `config._validate_addendum_layout` | base example `deploy_required`; dev example `joined_system_message` (J4a2 call 18); deploy overlay example `deploy_required` | `model_mapping.system_messages` via `openai_request` | configurable (deployment value) |
| `vlm.backends.<name>.addendum_separator` (non-empty string; reserved `none` with the separate layout only, decision 193) | same | base and overlay `deploy_required`; dev `"\n\n"` (decision 190) | same | configurable (deployment value); `none` protocol-bound (`config.NO_SEPARATOR`) |
| generate reply schema id `planner_reply_generate.v0` | new schema file | none (follows from `vlm.roles.planner.output.schema_id`, pinned to `planner_reply.v0` by `config.py:1837`) | `planner.py` | artifact-bound |
| `effective_system_hash` | call record (`record_json`), `writer_result.v1` | none | trace | not a setting (identity record) |
| `MODEL_CALL_REVISION` `model-call-3`, `GoldenAdapter.identity` `s0-s6.v2` | stage guard, activation record | none | K2-K4/kb_sync keys, activation | artifact-bound |

## Step 0

### 1. Unknowns and failure modes

- **U1 (cost).** `openai_request` and `_call` are in the `model_call` stage inventory (`tests/stage_guard.py:18-23`), so `MODEL_CALL_REVISION` goes to `model-call-3` (`model_client.py:32`). That revision keys the K2/K3/K4/kb_sync artifacts (`k2_naming.py:308`, `k2_observe.py:150`, `k3_cards.py:266`, `k4_aliases.py:184`, `kb_sync.py:189`), so the next `kb sync` recomputes them although their bodies do not change.
- **U2.** llama.cpp `maxItems` grammar support: the docs do not say (`docs/note/N055-llama-cpp-server.md:12`), and there is no network: **not verified**. The client guard `validate_response` (`model_client.py:875-876`) gives `SCHEMA_INVALID`, then one planner retry (`planner.py:314-318`).
- **U3.** The full Qwen3.5 template was not captured; J4a2 call 18 shows only that the joined layout is needed.
- **U4.** Fixtures built from `configs/enhancer.example.yaml` need a labelled test-only layout.

### 2. Questions (ranked, each with a proposed answer)

1. **`model-call-3` bump?** Yes: the layout must be applied per backend in the request builder, because `_call` can switch to `vlm.default_backend` (`model_client.py:715-725`, `:824-835`). Approved.
2. **Cloud and chat-less backends?** Refuse both keys. Anthropic/Gemini have one system channel with no order (`model_cloud.py:54-57`, `:62-66`), so the key would be an ignored setting. Refuse the separator with separate. Approved.
3. **Schema versioning?** I proposed additive fields in v0. **Superseded** by PM answer 3: v1 files (see "Versioning rule followed").
4. **Golden diagnostics path?** `PipelineResponse.diagnostics` plus `GoldenAdapter.run_case` (`s0-s6.v2`). Doubles without `run_case` write `null`. Rejected: changing what `run` returns (about 10 doubles, `tests/profile_fixtures.py:110-125`). Approved.
5. **Generate schema form?** A static `planner_reply_generate.v0`: `edit_kind` null, `maxItems: 0` on the four edit arrays (`prompts/planner_v1.txt:3`), with a derivation test. It is sent via `schema_id=` and applied after the probes, so `_proof_key` (`model_client.py:323-343`) is unchanged. Rejected: deriving it at run time (`model_schemas.py:81-92`). Approved.

### 3. Plan (approved; as built, see the Completion report)

- **A.** `addendum_layout` is required on `openai_compatible` chat backends (or a sentinel endpoint) and refused elsewhere. `addendum_separator` is required with joined, refused with separate, and allowed while the layout is a sentinel. `openai_request` (`model_mapping.py:83-87`) applies the backend's layout; with no addendum the body does not change. A missing or sentinel layout with an addendum raises `ConfigurationError` before HTTP. `effective_system_hash` is `sha256:` over the canonical JSON array of the system texts posted (OpenAI, Anthropic, Gemini). It is computed in `_call` and written to the call record and `writer_result.v1`, then into the trace. Examples: base `deploy_required` (**Superseded:** the base example has no separator key, only a comment, because `extends` cannot delete an inherited key, `config.py:715-722`; found by `test_k4_config`/`test_k5_config`). The dev example uses joined + `"\n\n"` (J4a2 call 18; PM decision pending user review). The overlay allows both keys (`config.py:771-772`).
- **B.** Generate calls `client.call(..., schema_id='planner_reply_generate.v0')`; edit is unchanged; the invariant `planner.py:109-111` stays.
- **C.** Planner `error.cause`. A fatal `ModelCallError` code goes into `diagnostics`. `PipelineResponse.diagnostics` holds `{planner, writer, validation, fallback_attempts, fatal, probe_reasons}`, codes only. `golden_results.json` gets `warnings`, `diagnostics`, and the exception `code`. The eval marker gets `code`. **Superseded:** `eval_run.py:339` stays. The envelope stays (it has no error field, R-024).
- **Docs.** `S4-writer.md:9,:31`, `enhancer.example.yaml:158`, `P-profiles.md:10`, `serving.md:37`, `S2-planner.md`, `S5-validate.md`, `run-guide.md:175`, `contracts/README.md`.
- **Finish checks.** Named files green through `test_slot.py`, one per process. Each finding has a replay, both layouts a property test, each key override/missing/sentinel tests. Plus the pin row and 3-10 mutants. The basetemp size is reported, then the basetemp removed.

## Steps

All DONE: (1) config keys, rules, examples; (2) mapping, `_call`, `model-call-3` pin, `writer_result.v1`/`validation_result.v1`; (3) generate schema, `plan_result.v1`; (4) cause codes, diagnostics, `run_case`, eval marker code; (5) docs, fixtures, tests; (6) sweep of other suites, mutants, report.

### Root causes (checked against the code during Step 0)

- **F1 confirmed (class b).** `model_mapping.py:83-87` appends the addendum as a second `system` message; `tests/test_model_addendum.py:37` pins it; `S4-writer.md:31` documents it.
- **F2 confirmed (class c plus b).** `planner.py:64-65` sends `creativity` (with `preserve`) for both tasks; `planner_reply.v0.schema.json:8-10` requires `preserve` and `:212-223` leaves it unbounded; `planner.py:109-111` refuses; one schema for both tasks (`model_client.py:774-782` takes `role['output']['schema_id']`).
- **F3 confirmed (class a for the lost reason).** `planner.py:311-312` returns `PLANNER_CALL_FAILED` and drops `error.code`; `plan_result.v0` `error` allows only `code`/`message`; `profiles.py:679-687` writes `status`, `passed`, `envelope_sha256`; `runs/T-017-pilot/profiles/pilot-v1/golden_results.json` holds exactly that for all three cases.
- **Writer HTTP 500 without repair or fallback output:** consistent with the documented contract, no fix. `docs/stages/S5-validate.md:34` "Refusal, truncation and operational binding or call failures go to fallback." `validation.py:325` breaks the repair loop when the draft is incomplete and not `SCHEMA_INVALID`; the fallbacks in order (`configs/enhancer.example.yaml:174`) are then unavailable for `pilot-v1`: no admin-confirmed minimal template (manifest has only `validator.checks: [single_line_minified_json]`), and the raw prompt fails that contract (`validation.py:340-347`), so `failed`. Exact `fallback_attempts` for J4a2 are not in the run evidence (no trace under `runs/`), so this is source-verified, not replayed.
- **S2 "degraded" text vs code:** the code is right about the outcome: `planner.py:342` returns plan state `degraded` with `intent_ir None`, the writer is skipped (`pipeline.py:231-235`) and the envelope is `failed`; `docs/stages/S2-planner.md:24` says the request "takes the path without knowledge with status degraded". Reported, not changed (out of scope).

## Pre-mortem and checks (one named test each)

| Failure | Check |
|---|---|
| Admin bytes changed, reordered or not first (either layout) | `test_model_addendum.py::test_admin_file_bytes_are_first_and_unchanged_in_either_layout[separate,joined]` (byte comparison against the admin file); property `test_joined_layout_property_admin_prefix_then_separator_then_addendum` |
| Separator missing, empty or sentinel when required | `test_model_addendum.py::test_unresolved_layout_with_an_addendum_refuses_before_http[no-separator,empty-separator,sentinel-separator]`; `test_addendum_layout_config.py::test_joined_layout_without_separator_refuses`, `::test_malformed_values_refuse_by_schema` |
| Separator applied with the separate layout | `test_addendum_layout_config.py::test_separator_with_separate_layout_refuses` |
| Backend with no layout key accepted silently | `test_addendum_layout_config.py::test_missing_layout_refuses_naming_key_backend_and_reason`; `test_writer_config.py::test_every_writer_leaf_refuses_before_call[*-vlm.backends.main.addendum_layout]`; request-time `test_unresolved_layout_with_an_addendum_refuses_before_http[missing,sentinel]` |
| S5 repair or fallback backend on a different layout | `test_validation_layout.py::test_s5_repair_sends_the_same_system_texts_as_the_draft[separate,joined]`; `test_model_addendum.py::test_budget_fallback_backend_uses_its_own_layout` |
| Edit schema accidentally narrowed | `test_planner_generate_schema.py::test_shared_edit_schema_is_not_narrowed`, `::test_edit_request_body_carries_the_shared_schema_and_accepts_preserve` |
| Generate still sends the shared schema on some path | `test_planner_generate_schema.py::test_generate_sends_the_generate_schema_on_the_first_call_and_the_retry`, `::test_generate_second_chance_sends_the_generate_schema`, `::test_budget_fallback_still_sends_the_generate_schema`, `::test_generate_request_body_carries_the_generate_schema`; probes send `PROBE_SCHEMA`, not a reply schema (override applied after probes, `model_client.py:668-674`) |
| Cause code lost on another path | `test_golden_diagnostics.py::test_real_golden_adapter_writes_the_planner_cause_and_probe_reasons`, `::test_runner_exception_records_class_and_checked_code`; `test_eval_concurrency.py::test_failure_marker_keeps_the_model_code_and_no_text` |
| Reply text leaking into golden results (R-026) | `test_golden_diagnostics.py::test_r026_marker_reaches_neither_golden_results_nor_diagnostics`, `::test_case_diagnostics_reduce_any_unrecognised_value`; `test_eval_concurrency.py::test_failure_marker_code_is_a_checked_token` |
| Trace hash over bytes other than those sent | `test_model_addendum.py::test_trace_hash_is_computed_over_the_system_texts_actually_sent[separate,joined,none]`, `::test_trace_hash_distinguishes_the_two_layouts`; cloud texts in `::test_native_cloud_system_parts_keep_admin_and_addendum_separate` |

Replays (labelled synthetic CPU fixtures, nothing copied from `runs/`): F1 `test_joined_layout_override_reaches_the_request` and the property test; F2 `test_replay_seven_locator_generate_reply_is_refused_by_the_client_schema`; F3 `test_real_golden_adapter_writes_the_planner_cause_and_probe_reasons`.

## Class sweep

| A: where system messages are built | Outcome |
|---|---|
| `model_mapping.openai_request` (`system_messages`) | fixed: per-backend layout |
| `model_cloud.cloud_request` (Anthropic, Gemini) | stays: native system channels; keys refused there |
| Probes, K2-K4, planner, verifier (no addendum) | stays: layout not read without an addendum (`test_both_layouts_without_addendum_send_the_admin_message_alone`) |
| S5 repair (`validation.py:328` -> `write`) | same layout, tested |
| Budget fallback (`_call` loop rebuilds the body per backend) | fallback's own layout, tested |

| B: where a planner reply schema is sent | Outcome |
|---|---|
| First call, retry, second chance, budget fallback (generate) | `planner_reply_generate.v0`, tested |
| Edit | `planner_reply.v0` (no override), tested |
| Probes and proof key | stay: probe schema, configured role schema in `_proof_key` |
| Other structured roles (writer per manifest, verifier, K2-K4) | not task-dependent; no change |

| C: where a failure code was dropped | Outcome |
|---|---|
| `planner.ask` | fixed: `error.cause` (`plan_result.v1`) |
| `pipeline._run_request` fatal `ModelCallError` | fixed: safe code in trace `diagnostics` beside the class |
| Writer call failure (`writer.py:175-176`) | stays: the code is already the call status; now in `diagnostics.writer.calls` |
| `profiles._check_locked` | fixed: `warnings`, `diagnostics`, exception `code` |
| `eval_run` failure marker | fixed: `code` (checked token) |
| `eval_run.py:339` metrics | stays: per-unit codes are in trace rows; a metrics change would bump `eval_result_metrics` and change stored rows |
| `envelope.py` | stays: no error field by contract (single honest status) |

## Fix round 1 (QA feedback; decisions 190 to 193)

Reviews: Gemini Flash PASS; Claude Opus PASS with 3 P3 (`.tmp/T017-FIX-REVIEW/review.md`). Intent first (decision 170): R-004, R-026, R-001, R-009 unchanged by every item below.

1. **Decision 193, separator `none`.** `config.NO_SEPARATOR = 'none'` (protocol-bound). `_validate_addendum_layout`: joined + `none` refused ("none sends no separator, and addendum_layout joined_system_message needs one ..."); separate accepts only an absent key or the exact `none`, and refuses any other value, including near spellings and sentinels. The schema adds `{"const": "none"}`. `system_messages` also refuses `none` under joined before HTTP, so `none` is never concatenated. The base example states `addendum_separator: deploy_required`. A sentinel layout or separator is refused at start by `_sentinel` (`validate_writer_reads`), whatever the other key holds, and is never defaulted. The deploy overlay still resolves (both keys `deploy_required`), and an overlay may state either layout. The dev example keeps joined + `"\n\n"`, now labelled decision 190. Lineage tests go through the real `_load_chain`: dev child → separate+`none`; dev child keeping `"\n\n"` → refused; separate parent → joined child; base child → either layout; base child stating only the layout → refused. Fixtures built from the base example state `none` (`test_loader.py`, `test_model_config.py`, `test_model_composition.py`). Docs: `S4-writer.md:31`, `contracts/vlm-request.md` (backends), `run-guide.md:86`, the schema description and the example comments.
   - **Stage pin:** the `system_messages` guard moves the `model_call` digest. The unmerged `model-call-3` row's digest was updated (`e800dead...`), not a new revision, because no stored artifact carries `model-call-3` yet and no valid configuration's request body changes. `MODEL_CALL_REVISION` is unchanged.
2. **Decision 192, `S2-planner.md` failure modes** rewritten to match `planner.py` `plan` and `pipeline.py` `_run_request`. Invalid reply: one retry, then plan `degraded`, `PLANNER_REPLY_INVALID`. Failed call: no retry, plan `failed`, `PLANNER_CALL_FAILED` with `error.cause`. Either way there is no IR, S4 does not run (`INTENT_UNAVAILABLE`), and S5 walks `validation.fallbacks`: envelope `degraded` with a contract-valid fallback output, otherwise `failed`. There is no path without knowledge. Line 15 (ambiguous target under `best_effort` → `degraded`) describes a usable plan, matches `planner.py:252`, and is kept. **Note for the PM:** decision 192 says "a planner failure fails the case"; the code does that only when no fallback yields contract-valid output (J4a2: none did). Pinned by `test_golden_diagnostics.py::test_planner_failure_skips_s4_and_ends_by_the_profile_fallbacks[failed,degraded]`. No behaviour change.
3. **F1:** `test_planner.py::test_failed_call_is_not_retried[http-error-record,incomplete-record,raised-model-call-error]` asserts `error.cause` and checks that no error text leaks.
4. **F2:** `test_golden_diagnostics.py::test_fatal_model_call_error_reaches_case_and_trace_as_its_code_only`. `compose_model_client` raises `PROBE_MALFORMED: <FATAL_MARKER>`; the test checks `diagnostics.fatal == ['PROBE_MALFORMED']`, the trace row `warnings` (`ModelCallError`, `PROBE_MALFORMED`), and that the marker appears in neither the case, the trace row nor `golden_results.json`.
5. **F3:** the joined property draws the separator from `TEXT` minus empty, `none` and sentinels, keeping the four fixed values as `@example`s.
6. **Open point 5 (PM decision pending user review):** `contracts/README.md` versioning sentence reworded. Draft configuration schemas take additive keys in place; stage and stored-artifact schemas get a new version. `profile check` hashes `profile_manifest.v0`, `golden_case.v0`, `response_envelope.v0` and `profile_validator.v0` (`profiles.py:62` `CHECK_SCHEMAS`; the review named three).

**Pre-mortem (decision 169), each class with its test:**

| Failure class | Test |
|---|---|
| `none` leaking into a joined message | `test_model_addendum.py::test_unresolved_layout_with_an_addendum_refuses_before_http[none-separator]`, `::test_none_separator_is_never_concatenated_under_the_separate_layout`; `test_addendum_layout_config.py::test_none_separator_with_separate_layout_sends_two_system_messages` |
| `none` accepted with joined | `test_none_separator_with_joined_layout_refuses` |
| A real separator silently ignored under separate | `test_separator_with_separate_layout_refuses[\n\n, ' ', None, NONE, ' none', none\n]`, `test_sentinel_separator_with_separate_layout_refuses` |
| A sentinel separator or layout defaulted instead of refused | `test_sentinel_layout_or_separator_refuses_at_start[*]`, `test_a_sentinel_layout_is_refused_at_start_whatever_the_separator[*]`, `test_the_base_example_addendum_keys_are_refused_at_start`, `test_a_child_of_the_base_example_that_states_only_the_layout_is_refused` |
| A child profile unable to switch layouts in either direction | `test_a_child_of_the_dev_example_switches_to_separate_with_none`, `test_a_child_of_a_separate_profile_switches_back_to_joined`, `test_a_child_of_the_base_example_can_choose_either_layout[*]`, `test_the_deploy_overlay_resolves_and_accepts_either_layout[*]` |
| Docs disagreeing with code | S2: `test_planner_failure_skips_s4_and_ends_by_the_profile_fallbacks[failed,degraded]`, `test_planner.py::test_invalid_reply_is_retried_once_then_degraded`, `::test_failed_call_is_not_retried[*]`; examples: `test_example_profiles_state_the_layout`; `test_run_guide.py` green |
| A test that passes on mutated code | mutants below, each with an unmutated control overlay that passed the same files |

**Mutants** (overlay package `.tmp/T017-FIX/mut/<id>/prompt_enhancer/` holding only the mutated module, put first on `PYTHONPATH`, with no tree copy; each run printed the overlay module path; the controls `C-config.py`, `C-model_mapping.py`, `C-pipeline.py` and `C-planner.py` passed every killing file):

| Id | Mutation | Killed by |
|---|---|---|
| X1 | `pipeline.py:335` fatal code not appended | `test_fatal_model_call_error_reaches_case_and_trace_as_its_code_only` |
| X3 | `planner.py:325` record cause set to None | `test_failed_call_is_not_retried[http-error-record,incomplete-record]` |
| N1 | joined + `none` refusal removed | `test_none_separator_with_joined_layout_refuses` |
| N2 | `system_messages` concatenates `none` | `test_unresolved_layout_with_an_addendum_refuses_before_http[none-separator]` |
| N3 | separate accepts a sentinel separator in the binding rule | `test_a_child_of_the_base_example_that_states_only_the_layout_is_refused` |
| N4 | separate accepts any separator | `test_separator_with_separate_layout_refuses[*]` (6), plus 2 lineage tests |

6 of 6 killed.

**Done-check runs** (`test_slot.py`, one file per process, basetemp `.tmp/T017-FIX/pytest`): addendum_layout_config 40, model_addendum 37, planner 67, golden_diagnostics 14, pipeline 18, pipeline_s0_s6 14, k0_config 33, loader 134, model_config 123, field_contracts 534 (+34 skipped), stage_revisions 403. Also run: model_composition 54, kb_sync_gates 21, validation_config 169, writer_config 215, real_contracts 3, cli_config 70, enhance_serve_config 197, k4_config 273, k5_config 52, validation_layout 2, run_guide 8, model_mapping 5, planner_generate_schema 18. All passed; `test_loader` and `test_model_config` first failed on fixtures built from the base example (the inherited `deploy_required` separator under separate) and passed once those fixtures stated `none`.

**Cleanup:** basetemp `.tmp/T017-FIX/pytest` was 523K and logs 94K, both removed with the scratch scripts and overlays; only `.tmp/T017-FIX/rca2-report.md` is kept. No Git writes.

**not_run (this round):** lint (Ruff not installed); the full suite; any other test files (not rerun this round); live model, GPU or network calls.

## Integration (phase 3, onto main 8935d11 = K3-RETRY + c8f1776)

Intent first (decision 170): R-001, R-004, R-009 and R-026 hold after the merge. K3's retry changes only `sampling.seed`, on a copy of the role (`sent_role`). `system_messages` and the admin-first layout are untouched. No retrieval path changed. Probe attempt records pass through `_safe_attempts`: codes, seeds, counters and digests only.

**How the 12 overlapping files were merged** (main's hunks from `.tmp/pm-state/patches/main-overlap.patch`, applied by hand with the Edit tool; no Git writes):

| File | Merge |
|---|---|
| `config.py` | Took K3's top-level `from .model_mapping import SAMPLING` and its four functions (`image_probe_backend`, `attempt_seed_refusal`, `_refuse_attempts_seed`, `_validate_probe_attempts`), placed after my `ADDENDUM_LAYOUTS`/`NO_SEPARATOR`/`_validate_addendum_layout` and before `_validate_model_bindings`. In `_validate_model_bindings(config, path, project_root=None)`, my per-backend addendum loop comes first and K3's `_validate_probe_attempts` call comes last. The seven callers take K3's `project_root`/`root` argument. Observe and cards take K3's `_refuse_attempts_seed` calls. |
| `model_mapping.py` (not overlapping, but changed by the merge) | My top-level `from .config import ...` made a cycle with K3's new `config -> model_mapping` import. It is now a lazy import inside `system_messages`, and the behaviour is unchanged. |
| `model_client.py` | The two `call` methods and `_call` take both my `schema_id`/`reply_schema` and K3's `seed`. `BoundModelClient.call` forwards each one only when it is set. K3's seed check runs first in `_call`. `sent_role` is built after my reply-schema override, so the override and the seed apply together. Also took K3's `_safe_attempts`, `_retry_plain_image`, the `image_attempts` limit check, the attempt log and the `sent_role` uses (openai/cloud request, `request_hash` sampling), plus the `attempt_seed_refusal` import. |
| `enhancer.example.yaml` | K3's `observe.reply_attempts`, `cards.reply_attempts` and `&model_probe image_attempts` (with comments), next to my `addendum_layout`/`addendum_separator: deploy_required`. |
| `enhancer.dev.example.yaml`, `overlays/deploy-vllm.example.yaml` | Every `probe` gets `image_attempts: fit_from_bench`; my addendum keys are kept. |
| `contracts/README.md` | K3's K3-RETRY sentence is appended to the configuration-version-2 paragraph; my reworded versioning sentence is kept. |
| `conftest.py`, `profile_fixtures.py`, `test_loader.py`, `test_model_composition.py`, `test_model_config.py` | Each fixture `probe` gets `image_attempts: 1`, plus `test_loader`'s `('probe', 'image_attempts', 'fit_from_bench')` sentinel case. Where my change had split the line to add the addendum keys, both edits sit on the same line. |
| `test_stage_revisions.py` | See Revisions. |

**Revisions.** Main's `model-call-3` row (`6c2b6e8b...`, K3-RETRY) is kept exactly as on main. My unmerged `model-call-3` row (`e800dead...`) is dropped. `MODEL_CALL_REVISION = 'model-call-4'`, and the new row is `3289be06c097f147e875604a7417bc884984ced2e0b9a312872566d479064a9b` (from `stage_guard.stage_digest('model_call')` on the merged tree). The other stages pass as pinned: K3's `k2-observe-3` and `k3-card-3` rows are intact, and `test_stage_revisions` passed all 415 tests. So no other stage both changes touch needs a new revision.

**Keys.** Every example profile carries `image_attempts` on each backend probe, and the base example carries `observe.reply_attempts` and `cards.reply_attempts`. `addendum_layout` and `addendum_separator` are in the base, dev and overlay examples. `enhancer_config.v0` holds both sets (reply_attempts, `probe.image_attempts`, addendum keys). `run-guide.md` lists both sets.

**Equality check** (scratch `.tmp/T017-FIX/integration_check.py`, read-only `git diff`/`git show`):
- `git diff 8935d11 --name-status` lists only paths my change touches, plus the 9 files main added that are untracked here. Those 9 are byte-equal to main (after LF normalisation): `reply_retry.py`, the K3 task file, 2 archive files and 5 K3 test files.
- For every one of my paths, the changed lines of `git diff 8935d11 -- <path>` equal those of `git diff --cached 445e3b7 -- <path>`.
- Exceptions: the overlap files listed above (the combined lines only); `model_mapping.py` (the lazy import); and this task file (this section plus a hook-written AUTO PROGRESS block).

**Tests** (`test_slot.py`, one file per process, `PYTHONPATH` = worktree `src;tests`, basetemp `.tmp/T017-FIX/pytest`). All 25 files passed:
- The 12 files' tests: stage_revisions 415, loader 135, model_composition 54, model_config 123.
- Fix-round files: addendum_layout_config 40, model_addendum 37, planner 67, golden_diagnostics 14, pipeline 18, pipeline_s0_s6 14, planner_generate_schema 18.
- K3-RETRY files: reply_retry 11, reply_retry_client 9, k2_retry 18, k3_retry 25, probe_retry 29, k0_config 33, k1_config 558, k2_probe 18, k3_cards 22, model_client 79, model_production_probes 84, kb_sync_production_probes 1, pipeline_s0_s3 1, field_contracts 540 (+34 skipped). No failures.

**Cleanup:** basetemp `.tmp/T017-FIX/pytest` (8.9M) and logs (32K) removed with the scratch scripts (`run.sh`, `integration_check.py`, `digest.py`); `.tmp/T017-FIX/rca2-report.md` kept.

**not_run:** the full suite (the PM runs it); lint (Ruff not installed); live model, GPU or network calls.

## Completion report

**Behaviour.** (A) `openai_compatible` chat backends must state `addendum_layout`; `joined_system_message` sends one system message `admin + addendum_separator + addendum`, `separate_system_message` keeps two; the admin bytes are an unchanged prefix in both; call records, `writer_result.v1` and the trace carry `effective_system_hash`. (B) Generate requests send `planner_reply_generate.v0`; the planner invariant stays. (C) `plan_result.v1.error.cause`, `PipelineResponse.diagnostics`, `GoldenAdapter.run_case` (`s0-s6.v2`) and `golden_results.json` keep codes per stage; eval markers keep the code.

**Intent check.** R-001: no prompt or IR rewriting added; the generate schema only removes edit fields the prompt already forbids. R-004: admin bytes verbatim and first (byte tests, property test, repair test). R-009: no retrieval path touched (planner retrieval tests green). R-026: only checked tokens reach diagnostics, golden results and markers (marker tests).

**Commands** (each `tools/agent-workflow/test_slot.py -- <venv python> -m pytest -q -p no:cacheprovider --basetemp .tmp/T017-FIX/pytest <file>`, `PYTHONPATH=<worktree>/src`, one file per process). Final results: model_addendum 35, model_mapping 5, model_cloud 10, addendum_layout_config 22, planner 67, planner_config 26, planner_generate_schema 18, writer 17, writer_attachments 18, writer_config 215, validation 44, validation_config 169, validation_layout 2, validator_properties 26, profile_validators 43, profiles 192 (+2 skipped), profiles_anchored 22, profile_properties 27, pipeline 18, pipeline_config 62, pipeline_s0_s3 1, pipeline_s0_s6 14, pipeline_s4_s5 1, envelope 7, golden_diagnostics 11, eval_concurrency 13, eval_config 211, k0_config 33, model_schemas 14, stage_revisions 403, run_guide 8, field_contracts 533 (+34 skipped). Sweep (other suites that build configurations from the examples): model_config 123, model_composition 54, loader 134, model_client 79, model_transport 42, probe_request_settings 132, model_client_image_side 36, model_production_probes 84, k1_config 558, k1_embed 68, k2_config 310, k2_observe_config 193, k2_observe_edges 20, k2_semantic_keys 212, k4_config 273, k5_config 52, cli_config 70, cli_commands 12, cli_enhance 24, enhance_serve_config 197, enhance_serve 21, embed_server_config 160, embed_server_identity 27, fix01 60, fix02 221, fix03 13, real_contracts 3, run_settings 5, storage_config 10, storage_review_r1 42, retained_images 72, kb_sync_gates 21, kb_sync_flow 33, kb_sync_production_probes 1: all passed.

**Mutants** (scratch script, removed at handoff: one textual replacement on a copy of `src/prompt_enhancer` under `.tmp/T017-FIX/mut`, run with `PYTHONPATH` pointing to it; an unmutated control copy passed all four killing files): M1 addendum before admin, killed by `test_joined_layout_property_...`; M2 separator dropped, killed (same); M3 missing layout accepted, killed by `test_missing_layout_refuses_...`; M4 separator under separate accepted, killed by `test_separator_with_separate_layout_refuses`; M5 generate sends the shared schema, killed by `test_generate_sends_the_generate_schema_...`; M6 `_call` ignores the override, killed by `test_generate_request_body_carries_the_generate_schema`; M7 planner cause dropped, killed by `test_real_golden_adapter_...`; M8 hash over all messages, killed by `test_trace_hash_is_computed_...[separate]`; M9 golden diagnostics dropped, killed by `test_real_golden_adapter_...`. 9 of 9 killed.

**not_run.** Lint (Ruff not installed). The full suite (forbidden). Live server, GPU or model call: whether llama.cpp's grammar enforces `maxItems: 0`, and whether the Qwen3.5 template accepts the joined message, are **not verified**; J4a2 needs a live re-run by the PM.

### Configuration audit

| Setting | Validated | Example | Override test | Missing / sentinel refusal |
|---|---|---|---|---|
| `addendum_layout` | schema enum + `_validate_addendum_layout` | base and overlay `deploy_required`; dev `joined_system_message` | `test_joined_layout_override_reaches_the_request`, `test_default_test_layout_sends_two_system_messages` | `test_missing_layout_...`; `test_sentinel_layout_or_separator_refuses_at_start[addendum_layout]`; cloud and chat-less refusals `test_cloud_backend_refuses_both_keys`, `test_chatless_backend_refuses_both_keys` |
| `addendum_separator` | schema: non-empty string, `{"const": "none"}` or sentinel; rules in `_validate_addendum_layout`; request-time guard in `system_messages` | dev `"\n\n"` (decision 190); base and overlay `deploy_required` (decision 193) | `test_joined_layout_override_reaches_the_request[\r\n---\r\n]`; `none`: `test_a_child_of_the_dev_example_switches_to_separate_with_none`, `test_none_separator_with_separate_layout_sends_two_system_messages`; switch back `test_a_child_of_a_separate_profile_switches_back_to_joined` | `test_joined_layout_without_separator_refuses`; `test_sentinel_layout_or_separator_refuses_at_start[addendum_separator-*]`; `test_a_sentinel_layout_is_refused_at_start_whatever_the_separator`; `test_the_base_example_addendum_keys_are_refused_at_start`; `none` with joined: `test_none_separator_with_joined_layout_refuses`, `test_unresolved_layout_with_an_addendum_refuses_before_http[none-separator]`; a real string with separate: `test_separator_with_separate_layout_refuses[*]`, `test_sentinel_separator_with_separate_layout_refuses` |
| Retained literals | `planner_reply_generate.v0` id artifact-bound; `s0-s6.v2`, `model-call-4` (after integration; see Integration) artifact-bound; `UNRECOGNISED` and the token alphabets in `pipeline._token` protocol-bound; separator `none` (`config.NO_SEPARATOR`) protocol-bound | | | |

### Changed files

Runtime: `config.py`, `model_mapping.py`, `model_client.py`, `planner.py`, `writer.py`, `validation.py`, `assembly.py`, `pipeline.py`, `profiles.py`, `eval_run.py`. Contracts: `enhancer_config.v0` (in place), new `plan_result.v1`, `writer_result.v1`, `validation_result.v1`, `planner_reply_generate.v0`; `contracts/README.md`. Configs: base, dev, deploy-vllm overlay examples. Docs: `S2-planner.md`, `S4-writer.md`, `S5-validate.md`, `P-profiles.md`, `serving.md`, `run-guide.md`. Tests: 4 new files, 20 edited (fixtures set a labelled test-only layout; cloud and embedder copies drop the keys).

### Noticed, not done

- Writer HTTP 500 with no repair or fallback output: matches the documented contract (`S5-validate.md:34`); see Root causes.
- Resolved in fix round 1: the S2 "degraded" text (decision 192), the child that could not switch to separate (decision 193), and the in-place `enhancer_config.v0` edit (README rule reworded; PM decision pending user review).
- Dropping the plain `image` probe requirement: not done here (decision 191 keeps it). A two-system-message probe: not added. `creativity.preserve`: not renamed.
- The verifier calibration identity (`semantic_role_settings`) does not include the layout.
- Eval result rows store the envelope output by contract (`eval_result_metrics.v0`), so the R-026 eval test covers the failing unit only.
- Merge: K3-RETRY also bumps `MODEL_CALL_REVISION`; resolved in phase 3 (`model-call-4`, see Integration).
