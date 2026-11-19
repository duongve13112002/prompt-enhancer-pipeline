# T017-FIX2: seeded planner retry and writer repair, shortlist-bounded planner reply schema

Kind: code. R-IDs: R-001, R-004, R-009, R-013, R-015, R-026, R-027. Source: `.tmp/T017-FIX2/rca3-report.md` (G3, G4).
Status: PM decision pending user review (the user is away; options C for G4 and 1 for G3 chosen by the PM).

## Step 0

**Unknowns and failure modes.**
- Planner and S5 declare no stage revision: `stage_guard.STAGES` covers only offline stages (k0..k5, kb_sync, `model_call`; `tests/stage_guard.py:45-118`). The only pinned code this task must edit is `RoleClient._call` (in `MODEL_CALL`, `stage_guard.py:18`), so the bump is `model-call-4` -> `model-call-5` with an appended pin row. Cost: K2/K3/K4 artifacts recompute once at the next `kb sync` (`docs/execution.md:81`), as K3-RETRY (`model-call-3`) and T017-FIX (`model-call-4`) already did.
- `planner.py` cannot import `reply_retry` at module level: `reply_retry.py:10` imports `planner.SEED_BITS`. A lazy import inside `plan()` follows `model_client.py:424`.
- Adding a function to `reply_retry` would be an unclassified name in a module pinned by k2_observe and k3 (`RETRY_SHARED`, `stage_guard.py:42-44`) and would force K2/K3 bumps; the seed-drop helper therefore lives in `planner.py`/`writer.py`, which no stage covers.
- Test doubles whose `call()` lacks `seed`/`schema` keywords (`planner_fixtures.py:23`): `seed` is passed only for a retry (as `BoundModelClient.call` already does, `model_client.py:263`); the planner double gains `schema`.

**Questions (each with the answer taken; none changes product meaning).**
1. *A role with no integer seed* (`sampling.seed` `none`/null is allowed by `vlm_role.v0` lines 125-128; the test profile uses `seed='none'`, `tests/conftest.py:43`). Answer: unseeded stays unseeded; the retry or repair round is the plain resend it is today, with `seed: null` in its attempt entry. The config does not refuse it: without a seed the backend samples afresh, so a resend is a new draw, and requiring a seed would contradict decision 186's dev option A. Tested.
2. *A backend that drops `sampling.seed`* (`PARAM_UNSUPPORTED`/`sampling.seed` in the record, `model_mapping.py:41-43`; anthropic maps no seed, `model_mapping.py:14`). Answer: the same refusal as `call_until_valid` (`reply_retry.py:57-59`), code `CONFIGURATION_INVALID`, with one justified difference: it applies only when a retry or repair is due, never to attempt 0. K2/K3 attempts are a configured count, so the whole configuration is invalid; the planner retry is fixed by decision 192 and the writer repair is `validation.max_repairs`, and refusing a valid first reply would change attempt 0. Planner: no retry is sent, the plan is `failed` with `PLANNER_CALL_FAILED`/`cause: CONFIGURATION_INVALID`. Writer: no repair is sent, the repair round records a `CONFIGURATION_INVALID` call and S5 goes to its fallbacks. A retry whose own record shows the drop (a budget fallback onto a seedless backend) is refused the same way. Tested. *Superseded for the writer in fix round 1 (F1): the writer repair is never refused. Only the planner keeps this answer.*
3. *Where the seed is recorded.* Answer: each planner call record in `plan_result.calls` and each writer `record_json` get `attempt: {attempt, seed, code, reply_digest}`, the `_safe_attempts` shape (`model_client.py:45-53`); `plan_result.v1` call items are open objects (`plan_result.v1.schema.json:69-79`) and `record_json` is a string, so no contract version changes. Both reach the trace through `build_trace_row` (`envelope.py:64`).
4. *Does bounding `links` break the edit case's `no_match` link with an empty shortlist?* Answer: no. A `no_match` link has `candidate_position: null`, `candidates: []`, `variants: []` (`check_reply` requires it, `planner.py:151-153`); the derived schema bounds only those three, never `links` itself, so the link stays valid. Tested with `('珍珠挂链', 'no_match', None, [])`'s shape (synthetic mention).
5. *Does llama.cpp's grammar honour `maxItems`/`maximum`?* Answer: not verified. N055 (`docs/note/N055-llama-cpp-server.md:12`) documents only that `response_format.json_schema` is read; `docs/implementation-notes.md:125` already lists `maxItems: 0` as not verified. No network: client-side validation against the sent schema is the guarantee.

**Plan.**
1. (A) `planner.ask()`: attempt 1 sends `seed=attempt_seed(base, 1)` when the base is an integer; refusal on a dropped seed; attempt entries on each call record.
2. (A) `writer.write(..., attempt=k)`: repair round k sends `attempt_seed(base, k)`; S5 passes `attempt=repairs` (the refusal planned here was removed in fix round 1).
3. (B) `planner.bounded_reply_schema()`: deep copy of the registered schema with only `maximum`/`maxItems` added (min with any existing) or the integer branch of a nullable position removed; sent through the one existing path: `BoundModelClient.call`/`RoleClient.call(schema=...)` -> `_call` (response_format, fallback loop, client validation against base and sent schema, `sent_schema_hash` in the record).
4. Pin row `model-call-5`, `SchemaRegistry.validate_with` pinned in `MODEL_CALL`; docs S2, S4, S5, `reply_retry` docstring.
5. Tests: replay per finding, properties, overrides of existing tests whose expectation (B) changes; mutants on scratch copies.

**Finish checks.** Done check items map to: pins green (`test_stage_revisions.py`); new tests in `tests/test_t017_fix2.py`; the listed test files green one per process; this file complete.

## Root causes, confirmed

- **G4 confirmed (source-verified + experiment).** Before this change `planner.ask()` looped `for _ in range(2)` over one `text` and one `options` dict and never passed `seed` (`planner.py` at base, lines 315-334); `RoleClient._call` changes the sent seed only when `seed` is given (`model_client.py:870-875` at base). So under a fixed `sampling.seed` the retry body equals attempt 0. Mutant M1 (the retry reuses the base seed, i.e. the old behaviour) is killed by `test_replay_g4_planner_retry_differs_from_attempt_0_only_in_seed`. The S5 repair had the same seed on every round (`validation.py` at base, line 328: `write(...)` with no seed); its brief differs only by the repair data.
- **G3 confirmed (source-verified + experiment).** `knowledge_needs.items.candidate_position` in both planner schemas is `{"type": "integer", "minimum": 0}` with no upper bound; `check_reply` refuses the need (`planner.py:167-168` at base, now 230-231). `test_replay_g3_...` asserts the registered `planner_reply_generate.v0` accepts the invented-need reply (`REGISTRY.validate` passes) and only the sent schema refuses it.

## Class sweep

**(A) in-product retries (re-checked).**

| Retry | Site | What varies now | Changed here |
|---|---|---|---|
| K2 observe | `k2_observe.py:330-341` -> `reply_retry.call_until_valid` | seed `attempt_seed(base, k)` | no |
| K3 cards | `k3_cards.py` -> `call_until_valid` (`cards.reply_attempts`) | same | no |
| Plain image probe | `model_client.py` `_retry_plain_image` (`image_attempts`) | borrowed role seed `+k`; dropped seed refused | no |
| Planner `ask()` | `planner.py` `plan.ask` (fixed 2) | **seed `attempt_seed(base, 1)`**; dropped seed refused when due | yes |
| Writer repair (S5) | `validation.py` repair loop, `writer.write(attempt=k)` | **seed `attempt_seed(base, k)`** + repair data; a dropped seed is recorded as null, never refused (fix round 1) | yes |
| Second-chance lookup | `planner.py` `plan` loop | a different shortlist, not a retry | no |
| HTTP transport | `model_transport.py:61` `http.max_retries` | nothing, by design (transport failure) | no |
| No retry | `k2_naming.py`, `k4_aliases.py`, profile drafter (`profiles.py`), S5 verifier (`validation.py`) | - | no |

**(B) where the planner schema is chosen, sent or validated.**

| Site | Role | After this change |
|---|---|---|
| `config.py:1990` `validate_planner_reads` | requires `json_schema` + `planner_reply.v0` | unchanged (the base contract) |
| `planner.py` `plan`: `reply_schema`, `options` | chooses generate/edit base | unchanged; plus `bounded_reply_schema` per `ask` (both first ask and second chance) |
| `BoundModelClient.call` / `RoleClient.call` / `_call` | override id, response_format, validation | `schema=` derived schema: body, every fallback iteration, validation after the base, `sent_schema_hash` |
| `model_mapping.openai_request` | puts `schema` into `response_format` | unchanged; receives the derived object |
| `model_cloud.cloud_request` | refuses structured output on cloud | unchanged (anthropic branch fed the derived schema for completeness) |
| `planner.check_integers` / `check_reply` | defence on the base schema / invariants | unchanged |
| `RoleClient._proof_key` / probes | configured role schema | unchanged (probe keys stay those of the configured role) |

**(B) other roles whose reply names positions of a sent list (reported, not fixed).**

| Role | Field | Defence today |
|---|---|---|
| observer (K2) | `observer_reply.v1` `concept_position` | `k2_observe.py:121-123` refuses out of range; K2 seed retry |
| card_builder (K3) | `card_builder_reply.v0` `facet_position`, `facet_positions`, `observation_positions`, `curated_positions` | `k3_cards.py:66-75` exact coverage checks; K3 seed retry |
| info_assessor (K2) | `info_assessment.v0` `position` | code verifies exact coverage of the declared positions (schema description, line 22) |
| verifier (S5) | `verifier_result.v0` `constraint_id` (ids, not positions) | `validate_verifier_reply` exact coverage |

## Pre-mortem

| Failure | Test that rules it out |
|---|---|
| attempt 0 changed | `test_attempt_0_is_the_request_the_old_path_builds` (body equals a direct old-path call once the (B) schema is put back; no `seed` keyword on attempt 0); `test_writer_attempt_0_sends_no_seed_keyword_and_records_the_base`; mutant M7 |
| retry seed equal to base, or repair seeds not pairwise distinct | `test_replay_g4_...` (M1); `test_writer_repair_rounds_send_distinct_seeds_and_record_them` (M8); `test_property_attempt_seeds_are_pairwise_distinct` |
| a seed sent to a role that had none | `test_unseeded_planner_retry_stays_a_plain_resend`, `test_unseeded_writer_repair_is_a_plain_resend` (M10) |
| silent repeat when the backend drops the seed | `test_dropped_seed_refuses_the_retry_instead_of_repeating_it` (M2), `test_retry_record_that_dropped_the_seed_is_refused`, `test_real_backend_without_seed_mapping_refuses_the_retry`; and a valid attempt 0 is kept: `test_dropped_seed_does_not_touch_a_valid_attempt_0` (planner only) |
| writer repair turned off on a seedless backend (fix round 1, F1) | `test_writer_repair_still_runs_when_the_draft_request_dropped_the_seed`, `test_seeded_writer_on_a_seedless_backend_still_repairs_and_records_no_sent_seed` (R1) |
| derived schema loosens a bound or narrows a valid reply | `test_derived_schema_only_tightens_and_bounds_every_position`, `test_property_derived_schema_accepts_every_reply_the_base_and_check_reply_accept`, `test_edit_reply_with_valid_links_and_needs_is_still_accepted`, `test_no_match_link_with_an_empty_shortlist_is_still_accepted` |
| out-of-range position still accepted | `test_property_derived_schema_rejects_every_out_of_range_position` (M3, M4) |
| derived schema misses the fallback backend, the retry or client validation | `test_budget_fallback_and_retry_send_the_derived_schema`, `test_replay_g3_...` (M5, M6) |
| sent-schema hash over other bytes | `test_sent_schema_hash_is_over_the_object_in_the_posted_body_and_the_record_names_the_base` |
| reply, prompt or KB text in records (R-026) | attempt entries hold counters, closed codes and a 64-hex digest only (asserted in `test_replay_g4_...`); `sent_schema_hash` is a digest; no reply text is added anywhere |
| admin system prompt touched (R-004) | the writer path changes only `seed`; `test_structure_precedes_verifier` (system bytes) and `test_validation_layout.py` still pass; the planner prompt file is not edited |
| user's prompt spans altered (R-001) | `check_reply` verbatim rules unchanged; the derived schema adds no rule over strings (`tightened_only` allows only `maximum`/`maxItems`/removed anyOf branches) |

## Steps

The plan above was carried out as written (see Behaviour). Notes that appear nowhere else:
- Pin row `model-call-5` digest `8371647d...`. The other stage digests were checked unchanged with a scratch script.
- The `PlannerClientDouble` test double now accepts `schema`/`seed`. Two T017-FIX body assertions in `test_planner_generate_schema.py` now expect the bounded schema.
- First run of the new tests: 5 test-side failures, all fixed:
  - calibrate binds the writer seed, so the tests now set it first;
  - the G4 replay now uses a `NOT_VERBATIM` span;
  - labelled probes consumed the fallback transport.
- The R-015 lint refused a literal code tuple. It was replaced by the `SEED_DROPPED` constant.

## Completion report

**Behaviour.**
- (A) G4. With an integer `sampling.seed`, the planner retry sends `seed=attempt_seed(base, 1)` through `RoleClient.call(seed=...)`, the existing override path (`planner.py:397`). Its text, images, system prompt and schema are byte-identical to attempt 0, and no validator message or earlier reply is sent back (R-026). Attempt 0 is the same request as before. S5 repair round k calls `write(..., attempt=k)` (`validation.py:339`), which sends `attempt_seed(base, k)` (`writer.attempt_seed_of`, `writer.py:161`); the repair data in the brief is unchanged. An unseeded role stays unseeded: the retry is a plain resend and records `seed: null`. When the planner retry is due and the previous record shows `PARAM_UNSUPPORTED`/`sampling.seed`, nothing is sent and the planner returns `failed` with `PLANNER_CALL_FAILED`/`CONFIGURATION_INVALID`. A planner retry whose own record dropped the seed is refused the same way. The writer repair is never refused for a dropped seed (fix round 1): it keeps the warning and records the sent seed as null. Every planner call record and writer `record_json` carries `attempt: {attempt, seed, code, reply_digest}` and reaches the trace through `build_trace_row`.
- (B) G3. Each `ask` builds `bounded_reply_schema(registry, base_id, shortlist, variants)` (`planner.py:121`), a deep copy of `planner_reply_generate.v0` or `planner_reply.v0`. The copy only adds `maximum` (min with any existing one) or `maxItems: 0`, or removes the integer branch of a nullable position, for: `knowledge_needs`, the `links` positions, `candidates`, `variants` and `ratings`. It adds no field and no rule over strings. It travels the one existing path, `RoleClient.call(schema=...)` -> `_call(sent_schema=...)`:
  - `check_schema` runs first;
  - the schema goes into `response_format` on every backend iteration, including budget fallbacks;
  - the reply is validated against the base schema and then the sent schema (`model_client.py:998`);
  - the record keeps `schema_id` (the base) and adds the derived schema's digest, with the same `_digest` convention as `effective_body_hash`. Since fix round 1 (N1) it is named `sent_schema_hash` only when the schema is placed in the body, and `client_schema_hash` when only the client checks it.
  The registered files, `check_integers` and `check_reply` are unchanged. A `no_match` link stays valid with an empty shortlist.
- Stage pins: `MODEL_CALL_REVISION` `model-call-5` (`model_client.py:33`), pin row appended, `SchemaRegistry.validate_with` pinned in `MODEL_CALL`. No other stage digest changed.

**Intent check.**

| Rule | Result |
|---|---|
| R-001 | No string rule added. The verbatim checks in `check_reply` are unchanged. The retry changes only the seed. |
| R-004 | The admin system bytes are untouched. The writer repair changes only `sampling.seed`. `test_validation_layout.py` and `test_structure_precedes_verifier` pass. |
| R-009 | Retrieval off sends no shortlist. The derived schema then has `maxItems: 0` on needs and candidates, so it reaches no knowledge base. No KB read was added. |
| R-026 | No validator text or reply text is fed back. Records hold counters, closed codes and a 64-hex digest only. |

**Research evidence.**

| Claim | Evidence | Label |
|---|---|---|
| The planner retry sent attempt 0 again | base `planner.py` `ask()` loop with no `seed`; `model_client.py` `_call` changes the seed only when it is given | source-verified; mutant M1 killed |
| The registered schema accepts an invented need | `test_replay_g3_...`: `REGISTRY.validate` passes and the sent schema refuses | experiment |
| Dropped-seed signal | `model_mapping.py:41-43` `PARAM_UNSUPPORTED`/`sampling.seed`; `reply_retry.py:57-59` refusal | source-verified |
| llama.cpp grammar honours `maxItems`/`maximum` | N055 line 12 documents only that `json_schema` is read | **not verified** |

**Configuration audit.** No configuration key was added or removed.

| Value | Class | Validated / tests |
|---|---|---|
| `vlm.roles.planner.sampling.seed` (existing) | configurable | seeded (non-default base) and unseeded (`'none'`) paths: `test_replay_g4_...`, `test_unseeded_planner_retry_stays_a_plain_resend` |
| `vlm.roles.writer.sampling.seed` (existing) | configurable | `test_writer_repair_rounds_send_distinct_seeds_and_record_them`, `test_unseeded_writer_repair_is_a_plain_resend` |
| `validation.max_repairs` (existing) | configurable | unchanged meaning; one seed per round (repair tests) |
| backend seed mapping / `seed_capability` (existing) | configurable | `test_real_backend_without_seed_mapping_refuses_the_retry` (refusal) |
| `vlm.roles.planner.output.schema_id` (existing) | configurable | base of the derived schema; `config.py` `validate_planner_reads` unchanged |
| planner retry count 2 | protocol-bound (decision 192) | unchanged |
| `SEED_DROPPED = 'CONFIGURATION_INVALID'` | protocol-bound (same code as `reply_retry`) | dropped-seed tests |
| `MODEL_CALL_REVISION = 'model-call-5'` | artifact-bound | `test_stage_revisions.py` |
| `maxItems: 0`, `count - 1` | protocol-bound (JSON Schema keywords, derived from the length of the sent list) | properties + M3/M4 |
| attempt entry keys | artifact-bound (`_safe_attempts` shape) | replay tests |

**Commands and output.** Each command is run as `$PY -B tools/agent-workflow/test_slot.py -- $PY -X faulthandler -m pytest -q -p no:cacheprovider --basetemp .tmp/T017-FIX2/pytest <file>`, one file per process. `$PY` is the project `.venv` (main checkout root), and the environment sets `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=<worktree>/src`. Last line per file, copied from the scratch logs, which were removed at handoff. The basetemp `.tmp/T017-FIX2/pytest` was 469 KB (369,561 bytes) at the end and was then removed.

Final state of the code, every file run after the last source edit:

| File | Result | File | Result |
|---|---|---|---|
| test_t017_fix2 | 32 passed | test_stage_revisions | 417 passed |
| test_planner | 67 passed | test_planner_config | 26 passed |
| test_planner_generate_schema | 18 passed | test_field_contracts | 540 passed, 34 skipped |
| test_validation | 44 passed | test_reply_retry | 11 passed |
| test_validation_layout | 2 passed | test_reply_retry_client | 9 passed |
| test_writer | 17 passed | test_model_client | 79 passed |
| test_writer_attachments | 18 passed | test_model_schemas | 14 passed |
| test_writer_config | 215 passed | test_pipeline | 18 passed |
| test_pipeline_config | 62 passed | test_pipeline_s0_s3 | 1 passed |
| test_pipeline_s0_s6 | 14 passed | test_pipeline_s4_s5 | 1 passed |
| test_profiles | 192 passed, 2 skipped | test_profiles_anchored | 22 passed |
| test_envelope | 7 passed | test_stage_flow | 24 passed |
| test_golden_diagnostics | 14 passed | test_model_addendum | 37 passed |
| test_model_production_probes | 84 passed | test_validator_properties | 26 passed |
| test_cli_enhance | 24 passed | test_decoder_boundaries | 10 passed |

`test_model_client_image_side` (36 passed) ran only before the last edits, which touched `planner.py`, `writer.py` and `validation.py`, none of them on its path. The R-015 lint failure in `test_planner` from the first run is fixed (see Steps). The skips are existing skips that this change does not touch.

**Mutants** (`.tmp` overlay copies, `tests/test_t017_fix2.py` + `tests/test_planner_generate_schema.py`, `-x`):

| Mutant | Change | Result | Killing test |
|---|---|---|---|
| M1 | planner retry seed = base seed (old behaviour) | killed | `test_replay_g4_planner_retry_differs_from_attempt_0_only_in_seed` |
| M2 | no dropped-seed check before the retry | killed | `test_dropped_seed_refuses_the_retry_instead_of_repeating_it` |
| M3 | `_cap` bound `count` instead of `count - 1` | killed | `test_property_derived_schema_rejects_every_out_of_range_position` |
| M4 | `knowledge_needs` never closed on an empty shortlist | killed | `test_derived_schema_only_tightens_and_bounds_every_position[counts0-planner_reply.v0]` |
| M5 | no client validation against the sent schema | killed | `test_replay_g3_invented_need_is_refused_by_the_sent_schema_client_side` |
| M6 | derived schema not put into `response_format` | killed | `test_replay_g3_...` |
| M7 | writer sends a seed on attempt 0 | killed | `test_writer_attempt_0_sends_no_seed_keyword_and_records_the_base` |
| M8 | every repair round sends round 1's seed | killed | `test_writer_repair_rounds_send_distinct_seeds_and_record_them` |
| M9 | `min` -> `max` with an existing `maximum` in `_cap` | survived, **equivalent today**: no base position node carries a `maximum`, so both give `count - 1`; `tightened_only` would catch it once one does | - |
| M10 | every role treated as seeded | killed | `test_replay_g3_...` (unseeded env; the unseeded-resend tests cover the same line) |

9 of 10 killed; log `.tmp/T017-FIX2/mut/result.log` (removed at handoff, figures copied here).

**not_run.**
- Lint: ruff is not installed.
- The full suite, per the brief.
- Any live model, GPU or llama.cpp run, so whether llama.cpp's grammar honours `maxItems`/`maximum` is not verified. Client validation against the sent schema is what guarantees the bounds.
- CI on Linux. The changed code holds no OS-specific logic.

**Changed files.** `src/prompt_enhancer/{model_client,model_schemas,planner,writer,validation,reply_retry}.py`, `tests/{stage_guard,test_stage_revisions,planner_fixtures,test_planner_generate_schema,test_t017_fix2}.py`, `docs/stages/{S2-planner,S4-writer,S5-validate}.md`, this file.

**Noticed, not done.**
- Out of scope per the brief: G1; a retrieval switch in golden cases; the S1 error code in the envelope; the planner's free-text language; a probe seed (option D); prompt text changes.
- `model-call-5` makes the K2/K3/K4 artifacts recompute once at the next `kb sync` (cost of pinning `_call`).
- Sibling roles whose replies name positions (observer, card_builder, info_assessor; table above) still send unbounded schemas. Their code defences and K2/K3 seed retry cover them, but the same derivation could tighten them later.
- Process deviation: one tracked test file was edited once through a scratch Python script (`.tmp/T017-FIX2/fix_tests.py`) instead of Edit. The result was re-read and all later edits used Edit.
- The planner's dropped-seed refusal applies only when the retry is due, unlike `call_until_valid`, which refuses at configuration time (Step 0 question 2). This is for the PM to confirm with the user.
- The planner's attempt entry still names the base seed for attempt 0 even when the record shows the backend dropped it. The writer entry now records null in that case (F1). Aligning the planner is a one-line change, left out because the PM scoped F1 to the writer.

## Fix round 1 (QA feedback; reviews: Gemini Flash PASS, Claude Sonnet PASS WITH NITS)

**F1 (done; PM decision pending user review).** The writer repair is no longer refused for a dropped seed. A repair brief carries the draft and the failures, so it never repeats the earlier request. The old refusal would have turned off repair that works today on a backend that maps no seed (`anthropic`, `model_mapping.py:12`).
- Removed: the S5 pre-repair check in `validation.py`, with its `SEED_DROPPED`/`drops_seed`/`attempt_seed_of` imports, and the post-call `SEED_DROPPED` status in `writer.write`.
- Kept: repair round k still sends `attempt_seed(base, k)`, and the attempt entry still records each attempt.
- Recording: when the record shows `PARAM_UNSUPPORTED`/`sampling.seed`, the entry's `seed` is null and the warning stays as today.
- The planner refusal is unchanged, because its retry is byte-identical. Both reviewers agree on this.
- Tests: the two refusal tests became `test_writer_repair_still_runs_when_the_draft_request_dropped_the_seed` and `test_seeded_writer_on_a_seedless_backend_still_repairs_and_records_no_sent_seed`. The second uses a labelled double of a seedless backend.
- Docs: `S5-validate.md`, `S4-writer.md` and the `reply_retry` docstring are updated.

**N1 (done).** `_call` names the digest of the derived schema after where the schema went:
- `sent_schema_hash` only when the schema is placed in the body (`json_schema` on `openai_compatible` or `anthropic`);
- `client_schema_hash` when only the client checks it (Gemini, `json_object`).
The same key enters `request_hash`. Tests: `test_schema_only_checked_client_side_is_recorded_as_client_schema_hash` (a `json_object` role: no schema in the body, the client still refuses an out-of-range reply) and `test_schema_placed_in_the_body_is_recorded_as_sent_schema_hash_only`. `S2-planner.md` is updated.

**N2 (kept, PM).** `drops_seed`/`SEED_DROPPED` stay in `planner.py`. Moving them to `reply_retry` would add a name to a module pinned by K2/K3 (`RETRY_SHARED`) and force K2/K3 pin bumps.

**N3 (done).** `writer.py` imports `planner.drops_seed` at the top. This is not circular, because `planner` imports no `writer`, and the tests are green. The `reply_retry` imports stay lazy, because `reply_retry` imports `planner.SEED_BITS`.

**N4 (done).** `test_second_chance_ask_widens_the_bound_to_the_larger_shortlist`: the first ask sends a 1-entry shortlist, the second chance a 2-entry one. The sent `links[].candidate_position` and `knowledge_needs[].candidate_position` maxima go from `[0]` to `[0, 1]`.

**Stage pin.** `_call` changed (N1), so `model-call-5` stays and its digest is re-pinned in place, because the row is not on main yet: `8371647d...` became `cd041a98fbd884f0d996ca7f0657abf063d701da3168e7ce35684639fb29c176`. Every other stage digest equals its last pin, and `unclassified()` returns `([], [], [])` (scratch script).

**Tests (one pytest process per file, same command as above):**

All 21 files pass. Passed counts per file:
- t017_fix2 35; planner 67; planner_config 26; planner_generate_schema 18;
- validation 44; validation_config 169; validation_layout 2;
- writer 17; writer_attachments 18; writer_config 215;
- model_client 79; model_client_image_side 36; model_schemas 14;
- stage_revisions 417; reply_retry 11; reply_retry_client 9;
- pipeline_s4_s5 1; pipeline_s0_s6 14; pipeline 18; validator_properties 26; envelope 7.

Not rerun in this round: `test_profiles*`, `test_stage_flow`, `test_golden_diagnostics`, `test_model_addendum`, `test_model_production_probes`, `test_cli_enhance`, `test_decoder_boundaries`, `test_field_contracts`, `test_pipeline_config` and `test_pipeline_s0_s3`. All passed in the first round, and none was rerun after the round 1 changes.

**Mutants (overlay copies, `tests/test_t017_fix2.py`, `-x`):**

| Mutant | Change | Result | Killing test |
|---|---|---|---|
| R1 | the writer records the seed even when the backend dropped it | killed | `test_seeded_writer_on_a_seedless_backend_still_repairs_and_records_no_sent_seed` |
| R2 | a client-only schema recorded as `sent_schema_hash` | killed | `test_schema_only_checked_client_side_is_recorded_as_client_schema_hash` |
| R3 | the second chance bounds to the old shortlist | killed | `test_second_chance_ask_widens_the_bound_to_the_larger_shortlist` |

3 of 3 killed.

**Cleanup.** The basetemp `.tmp/T017-FIX2/pytest` was 679 KB on disk (489,482 bytes) and is removed, along with the scratch scripts, overlays and logs. The PM's `rca3-report.md` and `review-request.md` are kept.

## Handoff (for docs/implementation-notes.md, PM to move)

- T017-FIX2 (PM decision pending user review): the planner retry and the S5 repair rounds change only `sampling.seed` (`attempt_seed(base, k)`). An unseeded role keeps a plain resend. For the planner only, a dropped seed is refused with `CONFIGURATION_INVALID`, and only when the retry is due. The writer repair still runs on a seedless backend and records the sent seed as null (fix round 1, F1, PM decision pending user review). Rejected: feeding validator feedback back (R-026); a refusal at start for unseeded roles (contradicts decision 186 option A).
- T017-FIX2: the planner sends a per-request schema derived from the registered one by tightening bounds only, through the existing `RoleClient.call(schema=...)` path. The record keeps the base `schema_id` plus `sent_schema_hash` (in the body) or `client_schema_hash` (client check only). Rejected: a second schema path; new registered schema files; content rules. Not verified: llama.cpp grammar enforcement of `maxItems`/`maximum`.
