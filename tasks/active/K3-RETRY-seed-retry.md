# K3-RETRY: retry with another seed for K3 cards, K2 observations and the plain image probe (decisions 183, 188)

Status: implemented and tested in this worktree, QA fix round 1 applied, uncommitted (no Git write made). R-IDs: R-001, R-004, R-009, R-015, R-017, R-018, R-026. Reply language to the PM: English.

## Phase 2 decisions (PM, approved)

Seed rule: attempt 0 is the profile's request unchanged; attempt k >= 1 sends `(role sampling.seed + k) mod 2^63` (`planner.SEED_BITS`); an integer seed is required when an attempts key exceeds 1; `model_mapping.SAMPLING` decides provider seed support (also the fallback backend); a call-time `PARAM_UNSUPPORTED` on `sampling.seed` is `CONFIGURATION_INVALID`, no retry. Retryable: a parse or schema refusal of a reply that arrived, and the client status `SCHEMA_INVALID`; never `INCOMPLETE_OR_REFUSED`, `HTTP_ERROR`, `ModelCallError`, `OSError`, `SOURCE_CHANGED`, cancellation; `PROBE_WRONG_ANSWER` stays a verdict. Resume is item level and attempts restart per run with the same seeds: a later run of the same item replaces its `k2_call_failure` or `k3_call_failure` row (`Checkpoints.commit` writes the one path of that item), so a failed attempt's record survives only until the next run of that item (a stored item keeps its attempts in its bundle); the seed experiment reads the failure rows after each run. `text` and `multiple_images` probes unchanged (class sweep, Noticed). Example sentinel is `fit_from_bench`. Added: artifact keys from the configured role, never the per-attempt copy; load-time refusal when `probe.image_attempts` overruns `probe.max_requests`; read the writer GC first.

### Configuration impact

| Setting | Schema / read set | Sentinel / refusal |
| --- | --- | --- |
| `cards.reply_attempts` (K3, per concept) | `enhancer_config.v0` `cards`, required, integer >= 1 or sentinel; `validate_card_reads` | sentinel refused naming the key; > 1 needs integer `card_builder.sampling.seed` and a seed-mapping provider (backend and budget fallback) |
| `observe.reply_attempts` (K2, per image) | `observe`, required; `validate_observation_reads` | same, role `observer`, on the backend its image call is served by |
| `vlm.backends.<n>.probe.image_attempts` (plain `image`, per challenge) | backend `probe`, required | sentinel refuses the whole backend (`_sentinel`); > 1 needs seed and provider for each image role served by that backend, `probe.max_requests` large enough (arithmetic in the message) and a readable probe bundle |
| `vlm.roles.<r>.sampling.seed` | unchanged schema; now the attempt base | integer required when an attempts key above 1 applies |
| seed modulus | `planner.SEED_BITS` 63, protocol-bound | none |
| request budget (R-018) | unchanged: a retry is a real request through the same ledger | `BUDGET_EXHAUSTED` never retried |
| concurrency | unchanged `max_concurrency` pool (`flow`) | none |

## Step 0 (condensed; approved)

Root cause (source-verified): each place failed on its first refused reply. K3: one `bound.call`, a `parse_card_reply` `ValueError` goes to `grouping.fail` and a `k3_call_failure` row with only the code; `kb_sync.require_success` then fails the stage. K2: one call, `parse_observation` fails the key and keeps no call record. Probe: `probe_verdict` refuses a code fence as `PROBE_MALFORMED`; the first verdict was cached as `negative` per role context, so K2 failed (J1b-L2). No request carried a seed; `vlm_role.v0` already allows an integer `sampling.seed`.

Design: `reply_retry.call_until_valid(call, parse, attempts, base_seed, log, persist)` inside each item's existing worker (no barrier, S46); `seed=None` for attempt 0; a per-request copy of the role carries the seed, so artifact and probe-proof keys come from the configured role; each attempt is `{attempt, seed, code, reply_digest}` (SHA-256 of the canonical reply, never text), persisted after every refused attempt. Plain `image` probe: `_retry_plain_image` re-sends only `PROBE_MALFORMED` challenges, each retry taken from `probe.max_requests`, `negative` only after the last attempt. Pins: `K3_CARD_REVISION k3-card-3`, `K2_OBSERVE_REVISION k2-observe-3`, `MODEL_CALL_REVISION model-call-3`; `kb-sync-6` digest unchanged.

GC (`store.py:1118-1151`): `_gc_artifacts` keeps an artifact only if a live snapshot or this session's unfinished build references it, so unreferenced `k2_call_failure` and `k3_call_failure` artifacts go after `sync.gc.retention`. Documented in K2, K3 and run-guide.

## Pre-mortem: one named test per item (all passed; files `test_reply_retry.py` RR, `test_reply_retry_client.py` RC, `test_k3_retry.py` K3, `test_k2_retry.py` K2, `test_probe_retry.py` PR)

| Item | Test |
| --- | --- |
| non-reply failure retried | K3/K2 `..._failures_that_are_not_a_refused_reply_are_never_retried`; RR `..._generated_outcome_sequences`; RC `..._an_exhausted_budget_is_not_retried` |
| same seed again, or more than the seed changed | RR `..._attempt_seeds_are_distinct_and_wrap...`; RC `..._a_retry_changes_only_the_seed...`; K3/K2 `..._stored_on_attempt_two...`; PR `test_a_retry_changes_only_the_seed` |
| validator text or previous reply reaches the next request | K3/K2 `..._carries_neither_validator_text_nor_the_previous_reply`; RR `..._digest_is_of_the_reply_and_never_its_text` |
| exhaustion publishes or stores an incomplete result | K3 `..._every_attempt_bad_fails_the_concept...`, `..._exhaustion_fails_the_sync_stage...`; K2 `..._every_attempt_bad_fails_the_image...` |
| more attempts than configured, or a budget shared across items | RR outcome sequences (`requests <= attempts`); K2 `..._attempts_are_per_image_not_shared`; PR `..._non_default_attempt_count...`. K3 has one concept per fixture: its per-item rule rests on the loop property |
| absent key or sentinel accepted | K3/K2 sentinel, missing and invalid-key tests; PR `..._missing_sentinel_and_invalid_image_attempts_are_refused`; `test_loader` row |
| resume re-asks a done item, or loses records after a crash | K3/K2 `..._found_by_the_next_run_with_zero_model_calls`, `..._a_failure_between_attempts_keeps_the_earlier_attempt_record`; K3 `..._artifact_keys_ignore_the_attempt_seed`; RR `persist` property |
| probe caches `negative` after the first failure, or drops failed attempts | PR `..._negative_only_after_the_last_attempt_fails[0..3]`, `..._pass_on_the_second_attempt...`, `..._unpaid_retry_is_incomplete_never_negative`, `..._wrong_answer_is_a_verdict...`, `..._reuses_the_proof_of_the_configured_role` |
| retries serialise the stage | K3 `..._retries_do_not_serialise_the_stage`: structural only (one `await flow(` site, no `gather`), one concept |
| backend without seed support | K3/K2 `..._provider_that_cannot_take_a_seed_is_refused...`; PR `..._without_an_integer_seed`, `..._names_both_keys`; RC and RR dropped-seed tests |

## Class sweep (every place a model reply is validated and refused in K2, K3 and the probes)

| Place | Verdict |
| --- | --- |
| `k3_cards.parse_card_reply`, `k2_observe.parse_observation`, client `SCHEMA_INVALID` (`model_client.py:962`), plain `image` probe `PROBE_MALFORMED` | covered |
| `text` and `multiple_images` probes | out of scope by decision (Q4); a fence can hit them too: Noticed |
| `json_schema` and `image+json_schema` probes | not retried: the request carries a grammar, so `PROBE_MALFORMED` means the server does not enforce it, which another seed does not cure (decision 188 names plain `image` only) |
| non-production probe paths (`local_probe_plans`, `probe_local_*`) | test-double paths, unchanged |
| K2 naming parsers, `k4_aliases.parse_alias_reply`, S2 planner (own retry), writer and verifier checks | out of scope by the decisions |
| `STORED_SCHEMA` checks (K3, K2), `kb_overrides.v0`, `strict_json` in `attachments`, `cli_input`, `eval_*`, `profile*`, `pipeline`, `verifier_policy` | code-built documents or user and stored files, not a K2, K3 or probe reply |

## Completion report

**Behaviour.** Three keys (`observe.reply_attempts`, `cards.reply_attempts`, `probe.image_attempts`, required, `fit_from_bench` in every example profile) set how many requests one image, concept or plain-image challenge may spend on a refused reply. The request is re-sent identical except `sampling.seed` (attempt k = base + k mod 2^63). No validator text, earlier reply, fence stripping or relaxed validation. Exhaustion fails the stage as before (nothing stored, nothing published). Every attempt is recorded and persisted before the next; `report['attempts']` gives `allowed` and `used` per item. Probe: `negative` only after the last attempt, failed attempts stay in the evidence; a retry the `probe.max_requests` allowance cannot pay is `PROBE_INCOMPLETE`, a retry that fails in transport is `PROBE_FAILED`. The loader refuses an attempts value above 1 without an integer seed, with a provider that cannot map `seed`, or (probe) beyond the allowance or without a readable bundle, naming the keys with the arithmetic (a floor: the role's own challenges plus retries; other roles on the same backend add at run time and stay a runtime `PROBE_INCOMPLETE`).

**Fix round 1 (QA: Claude Opus PASS with five P3, Gemini Flash PASS; F1 to F5 all fixed).** Root cause: F1, F2, F5 were gaps in tests or docs around correct behaviour; F3 and F4 were edge cases of the load-time and probe-time checks.
- F1 (`model_client.py:461`): `PR::test_a_transport_failure_on_a_retry_is_failed_never_negative_or_incomplete[1,2]` (HTTP 500 on attempt 1 or 2): status `failed`, `PROBE_FAILED`, never `negative`; the log keeps the failed entry. Mutant `errors[name] = 'PROBE_INCOMPLETE'` now killed (2 failed, 23 passed). No source change.
- F2: the allowance test uses `2 + count + count + 1`, asserts the request count and per-challenge logs, and its comment gives the real arithmetic (3 retry requests, not 1); the refusal regex is `vlm\.backends\.main\.probe\.image_attempts`; the K3 serialisation docstring says "structural only, one concept" and asserts one concept (no two-concept fixture built).
- F3: new `config.image_probe_backend(vlm, role)` (the condition of `_production_probe` and `_call`: a backend that is not `openai_compatible` and has no `allow_images` moves the image call to `default_backend`). `attempt_seed_refusal` takes `backend_name`; `_retry_plain_image`, `_validate_probe_attempts` (skips a role whose backend never probes; attempts, probe block, key and `max_requests` come from the probed backend) and `_refuse_attempts_seed` (K2 observer) use it. Tests: `PR::test_a_role_on_a_backend_that_does_not_image_probe_retries_on_the_backend_that_does` (load and run), `..._a_backend_that_never_image_probes_is_not_asked_for_a_seed`, `..._a_backend_that_image_probes_is_asked_for_a_seed` (control).
- F4: `_validate_probe_attempts` reads the probe bundle from the loader's project root (passed through `_validate_model_bindings`, all 7 callers) and refuses an unreadable bundle with `image_attempts > 1`, naming key, bundle and root; `PR::test_an_unreadable_probe_bundle_is_refused_never_skipped`.
- F5: one sentence in `K2-observe.md`, `K3-cards.md`, `run-guide.md`; the "earlier records stay" line above is corrected.
- Class sweep: callers of `attempt_seed_refusal` (K2 read set, `_validate_probe_attempts`, `_retry_plain_image`; K3 is text-only) and of `_validate_model_bindings` (7) all use the new parameter or root; `probe_image_challenges` has one caller. Pins: none changed (`_retry_plain_image` is in the left-out probe row, `RoleClient._call` untouched, config names left out).
- Mutants of this round, each killed: F1 above; F3a `_retry_plain_image` without `backend_name`, F3b `probed = role.get('backend')`, F4 unreadable bundle skipped (1 failed each, `mutplug.py` scratch plugin).

**Commands and output** (each through `tools/agent-workflow/test_slot.py`, one pytest process at a time, `--basetemp .tmp/K3-RETRY/pytest`):
- Fix round 1, all passed: `test_probe_retry` 29 (was 23), `test_k3_retry` 25, `test_k2_retry` 18, `test_stage_revisions` 409 (rows `model-call-3`, `k2-observe-3`, `k3-card-3`; `kb-sync-6` unchanged), `test_k0_config` 33, `test_model_config` 123, `test_loader` 135, `test_model_production_probes` 84, `test_kb_sync_production_probes` 1, `test_k2_observe_config` 193, `_shared_config` 137, `_shared_keys` 6, `test_k2_semantic_keys` 216, `test_k3_config` 283, `test_field_contracts` 538 (34 skipped), `test_validation_config` 169, `test_writer_config` 213, `test_model_client` 79, `test_kb_sync_flow` 33, `test_kb_sync_gates` 21.
- First round: the five new files 86 passed; the other suites (K1, K2, K3, K4, kb_sync, kb_audit, model, cli, pipeline, planner, stage_flow, profiles, eval_config) passed, counts in the first handoff. Failures met and fixed then: fixtures lacking the three keys, a sentinel in the K0 read set, the loop spec for a raised call, a boolean seed refused by the role schema, the writer lock.

**Intent check.** R-001: the request text, schema, images and every sampling field except `seed` are identical across attempts (`only_seed_differs` in K3, K2, probe, client tests); nothing about the reply or its refusal goes back. R-004: the admin system prompt bytes are untouched. R-009: retrieval off still makes no K2 or K3 call; the new keys are read only by `kb sync`. R-026: knowledge-base and image text stay data; attempt records hold a SHA-256 digest, never reply text (marker tests on requests and failure rows).

**Configuration audit.**

| Row | Evidence |
| --- | --- |
| `cards.reply_attempts` | schema (required), example profiles, override 9 valid, sentinel, missing, invalid refusals (K3 tests) |
| `observe.reply_attempts` | same, the K2 twins; `test_k2_observe_shared_config` read-set table |
| `probe.image_attempts` | schema, 3 example files (5 probe blocks), override 5, missing, sentinel, invalid refusals (the refusal message now names `vlm.backends.main.probe.image_attempts`); `test_loader` rows; probed-backend and bundle rows from fix round 1 |
| attempt seed base | `sampling.seed` integer in [0, 2^63) required above 1 (`none`, null, -1, 2^63 refused; boolean refused by the role schema) |
| literals | `REFUSED` tuple: structural, not a content rule; `SEED_WARNING`: protocol-bound; seed modulus (`SEED_BITS`): protocol-bound; no hidden default |

**Mutants of the first round** (`mutplug.py`, nothing in the repository written): all 10 killed. `m1` same seed, `m2` retry on `HTTP_ERROR`, `m3` one attempt too many, `m4` no persist: `RR` property. `m5` configured role receives the seed (two edits; the one-edit form is equivalent because `_call` already deep-copies the role): `RC::..._configured_role_is_untouched`. `m6` probe never retries, `m8` unpaid retry counted negative: `PR`. `m7` dropped-seed guard removed: `RR`. `m9` seed refusal off: `K3 ..._need_an_integer_seed[2]`. `m10` K3 failure row not persisted per attempt: `K3 ..._stored_on_attempt_two...`.

**not_run.** The full suite and test files outside the lists above; lint (ruff not installed); any live model, GPU or network call; the seed experiment itself (it sets the three values).

**Changed files.** Source: `config.py`, `model_client.py`, `model_probe.py`, `k2_observe.py`, `k3_cards.py`, new `reply_retry.py`. Contracts and configs: `enhancer_config.v0.schema.json`, `contracts/README.md`, the three example profiles. Docs: `K2-observe.md`, `K3-cards.md`, `VLM_CALL_RULES.md`, `run-guide.md`. Tests: five new files (`test_probe_retry.py` and `test_k3_retry.py` edited in fix round 1), `stage_guard.py`, `test_stage_revisions.py`, and fixtures for the new required keys (18 existing test and fixture files).

**Open issues.**
- The serialisation check is structural, not a concurrency test.
- A request that raises (budget, cancellation) gets no attempt entry; its code is in the stage failure row `error`.
- A sentinel in `probe.image_attempts` refuses the whole backend for every stage: by design, but a deploy profile must carry the integer.
- `basetemp` `.tmp/K3-RETRY/pytest` was 554 KB (whole scratch dir about 603 KB) before removal.

**Noticed, not done.**
- The `text` and `multiple_images` probes are prose-only and can be fenced like `image`; extend `probe.*_attempts` only if the experiment shows it.
- `runs/T-017` profiles need the three new required keys before they load.
- `kb gc` removes unreferenced `k2_call_failure` and `k3_call_failure` rows after `sync.gc.retention`, and a later run replaces them: read them first.
- K3 fixtures build one concept; a two-concept replay would show per-concept attempts directly.
