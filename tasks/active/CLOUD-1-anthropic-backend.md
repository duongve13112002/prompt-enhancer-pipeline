# CLOUD-1: the Anthropic backend sends structured output, honours Haiku 5.5 request rules, retries without a seed, stops at the request budget

Kind: code. R-IDs: R-001, R-004, R-013, R-015, R-018, R-026, R-027. Decisions 195-201. Approach note: `.tmp/CLOUD-1/approach.md` (scratch, removed at handoff).
Status: built and tested; waiting for the PM's review and the one-call live probe. Paths are relative to the worktree.

## Step 0 (approved by the PM with answers 1-6; condensed after approval)

### Root cause (confirmed in the code before editing)

- (A) `cloud_request` took `schema` and never used it, and `config.py` refused every structured Anthropic role. `model_client.py` named the T017-FIX2 bounded schema `sent_schema_hash` although nothing went into the body. `map_cloud_reasoning` wrote `output_config = {'effort': ...}`, which a format added by plain assignment would have overwritten.
- (C) T017-FIX2 assumed every backend has a seed field. Dropped-seed warning became a refusal in `config.attempt_seed_refusal`, `reply_retry.call_until_valid` (K2, K3, K4), `planner.ask`, and the probe retry in `model_client._retry_plain_image`. `writer.py` only records `seed: null`.
- Class sweep: `cloud_request` callers are `model_client` (every role call) and `model_probe.py:295` (probe bodies). The dropped-seed warning is produced once, in `model_mapping`.

### How `call_budget` is counted (D, source-verified)

Per process and per client run, never persisted: `ModelClientRun.__init__` builds `SingleRunBudgetLedger({name: call_budget})` (`model_client.py:188-189`; "In-process ledger; never share a grant across processes or restarts", `model_resources.py:21`). K2, K3, K4, kb_sync and the pipeline each build their own run, so 3000 is 3000 per run object, not in total; the PM tracks the total. One reservation per HTTP attempt: a transport retry costs one (`model_transport.py:60-76`) and probe requests use the same ledger. With `stop`, `BudgetExhausted` becomes `ModelCallError('BUDGET_EXHAUSTED: ...')` and nothing falls back to the local backend. So: no persistent ledger.

### Schema scan (A): `anthropic_output_schema` over `contracts/schemas/`

Pass (11): `planner_reply`, `planner_reply_generate`, `verifier_result`, `observer_reply` v0/v1, `info_assessment`, `folder_naming`, `card_builder_reply`, `alias_generator_reply`, `image_observation`, the probe schema. Refused at start (2, by `test_registered_schemas_that_cannot_be_sent_are_refused`): `profile_manifest.v0` (objects without `additionalProperties: false`), `eval_scenario.v0` (external `$ref` to `constraint_ledger.v0`). Kept and unverified until the PM's probe: `pattern` (image_observation), `$comment`, `type` arrays like `["boolean","null"]`.

### Questions and the PM's answers

1. `planner.refuses_retry` added; `drops_seed` and `writer.py` unchanged (answered: yes, minimal `planner.ask` edit). 2. `mode: off` maps to `thinking: {type: disabled}`; Haiku 5.5 refuses it above effort `high` (yes). 3. One-line change at `model_client.py:450` (yes; I went further, see Noticed). 4. Pilot role states `none` for sampling fields (yes; the role schema does not allow it, see Noticed). 5. Strip `minItems`, `maxItems`, `uniqueItems` too, test the client still refuses (yes). 6. Header `x-api-key`, no OAuth, fake key under `tmp_path` (yes).

## Steps

- Step 1 `model_mapping.py`, `model_cloud.py`: `provider_maps_seed`, `seed_field_note`, `ANTHROPIC_MODEL_REQUEST_RULES` (Haiku 5.5 row, source in comment), `anthropic_reasoning_refusal`, `off` mapping; `x-api-key` header; `ANTHROPIC_UNSUPPORTED_KEYWORDS`, `anthropic_output_schema` (positive applicators only), `output_config` merge, `extra_body.output_config` refused.
- Step 2 `config.py`: `_validate_anthropic_schemas`, Haiku refusal at load, `attempt_seed_refusal` needs an integer seed only when a reachable backend maps one.
- Step 3 `model_client.py` (hashes, probe retry), `planner.py`, `reply_retry.py`; revisions `model-call-6`, `k2-observe-4`, `k3-card-4` with PINS rows.
- Step 4 tests (`test_cloud1_anthropic.py` 79, `test_cloud1_planner.py` 7), existing tests moved to the new rules, docs (S2, S5, K2, K3, `VLM_CALL_RULES.md` item 11), example profile comments, wide run, mutants.

### Pre-mortem (final)

| Failure | Ruling-out test (`test_cloud1_anthropic.py` unless noted) |
|---|---|
| stripped copy loosens the client check | `test_stripping_can_loosen_and_the_client_side_check_still_restores_every_bound`, `test_a_reply_that_breaks_a_stripped_bound_is_still_refused_client_side`, `test_property_stripped_copy_accepts_every_instance_the_full_schema_accepts` |
| `output_config.effort` lost | `test_body_carries_format_merged_with_effort...`, `test_the_format_is_added_without_an_effort...`, `test_extra_body_cannot_replace_output_config...` |
| hash for bytes not sent | `test_hashes_name_the_bytes_sent_and_the_full_schema_separately`; planner flow in `test_cloud1_planner.py::test_real_client_planner_retry_on_anthropic_posts_the_same_unseeded_request_twice` |
| system or user bytes changed (R-004, R-001) | `test_body_carries_format_merged_with_effort_and_keeps_the_prompts_byte_for_byte` |
| seeded backend that drops the seed no longer refused | `test_a_seeded_protocol_that_drops_the_seed_is_still_refused`, `test_cloud1_planner.py::test_a_seeded_backend_that_drops_the_seed_still_refuses_the_planner_retry`, `test_k2_retry`/`test_k3_retry` seeded-fallback cases |
| Anthropic retry refused or sent too often | `test_anthropic_retry_is_a_fresh_unseeded_sample_with_the_configured_count`, `test_anthropic_retries_stop_at_the_configured_count`, planner `test_an_anthropic_retry_is_sent_once_unseeded_and_recorded_as_fresh` |
| `stop` falls back to local | `test_stop_ends_the_run_with_budget_exhausted_and_never_falls_back`, `test_stop_also_ends_a_retry_loop_at_once` |
| key read elsewhere or printed | `test_key_is_read_from_the_project_file_into_x_api_key_and_never_recorded`; planner flow asserts the key is absent from every call record |
| reply or KB text in records (R-026) | not newly tested: records still hold codes and digests only; existing `test_k2_r026` and planner tests stay green |

## Completion report

- **Work completed:** all four parts plus the six PM answers. (A) a role bound to `cloud_claude` with `output.format: json_schema` sends `output_config = {effort, format: {type: json_schema, schema: <stripped copy>}}`; the stripped copy drops only `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minLength`, `maxLength`, `minItems`, `maxItems`, `uniqueItems` (`model_cloud.py:61-94`), adds no key, refuses unsendable schemas at load. (B) Haiku 5.5 rules in a model-id table (`model_mapping.py`, refusal at `:52`). (C) seed rule keyed on `SAMPLING` through `provider_maps_seed`. (D) budget semantics documented and tested.
- **Result and evidence:** commands were `D:/.../.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <python> -m pytest -q -p no:cacheprovider <one file>`, `PYTHONPATH=<worktree>/src`, one file per process. All 51 files pass: cloud1_anthropic 79 (after adding the M2 test), cloud1_planner 7, config_links 10, field_contracts 540, k2_* 1,208 in 18 files (incl. k2_retry 19), k3_* 403 in 7 files (k3_retry 26), kb_sync_alias_failure 29, kb_sync_gates 21 (after the fix below; the first run had 1 failed, 20 passed), loader 135, model_addendum 37, model_client 79, model_client_image_side 36, model_cloud 10, model_composition 54, model_config 123, model_mapping 5, model_production_probes 84, planner 67, planner_config 26, planner_generate_schema 18, profiles 192, real_contracts 3, reply_retry 11, reply_retry_client 9, stage_revisions 433, t017_fix2 35, validation_config 169, writer_config 215. Last lines, for example: `79 passed in 14.32s` (cloud1_anthropic), `21 passed in 89.62s (0:01:29)` (kb_sync_gates). `eol_check.py`: `eol_check: clean`.
  Mutants (scratch copy `.tmp/CLOUD-1/mut`, `mutate.py`, each on its test file with `-x`): M1 strip nothing KILLED; M2 `additionalProperties` as a schema accepted: first SURVIVED (`77 passed`), killed after I added two refusal cases (`1 failed, 6 passed`); M3 format replaces effort KILLED; M4 `Authorization: Bearer` again KILLED; M5 Anthropic retry refused KILLED; M6 seed always sent KILLED; M7 planner refuses every drop KILLED; M8 effort rule inverted KILLED; M9 `sent_schema_hash` over the full schema KILLED; M10 load seed check disabled KILLED; M11 `off` not mapped KILLED; M12 marker dropped KILLED. 12 of 12 killed.
  Envelope trace (read, not run end to end): a planner call stopped by the budget raises `ModelCallError` with code `BUDGET_EXHAUSTED`; `planner.py:414` returns `PLANNER_CALL_FAILED` with that cause; the plan result is `state: failed`, `error: {code: PLANNER_CALL_FAILED, cause: BUDGET_EXHAUSTED}`, `calls: []` (existing `test_planner.py::test_failed_call_is_not_retried[raised-model-call-error]`). `pipeline.case_diagnostics` copies code and cause (`pipeline.py:83-86`); `envelope.build_envelope` gives `status: failed`, `output: null` (`envelope.py:32-39`). The envelope `warnings` list does not carry `BUDGET_EXHAUSTED` for this path (the planner adds only `SCHEMA_INVALID`); `validation.py:323` sets `degraded` only for a validator-stage warning. I changed none of this.
- **Important changes and reasons:** `x-api-key` replaces `Authorization: Bearer` (protocol-bound, source in a comment). `reasoning.mode: off` now sends `thinking: {type: disabled}`; before it was a warning only. A structured Anthropic probe now sends a structured body (the probe path uses `cloud_request`). Revisions bumped: `model-call-6`, `k2-observe-4`, `k3-card-4` (the first is in every K2, K3, K4, kb_sync key, so the other two cost no extra recomputation). `test_kb_sync_gates` had one case asserting the old refusal for Anthropic; it now expects Gemini refused and Anthropic to reach K0.
- **Problems, risks and remaining work:** below. Not run: the live API call (the PM's one-call probe), `ruff` (installed config covers `tools/agent-workflow` only; no `ruff` in `.venv`), the full tool suite, tests outside the 51 files, `pre-push` checks.

Done-check output (one pytest process per file through `test_slot.py`; the log is `.tmp/CLOUD-1/runall.log`, removed at handoff):

```text
tests/test_cloud1_anthropic.py        79 passed in 14.32s
tests/test_cloud1_planner.py           7 passed in 4.38s
tests/test_kb_sync_gates.py           21 passed in 89.62s (0:01:29)
48 other files in the set             every file: all passed (counts above)
mutants M1-M12                        12 killed, 0 survived (M2 after one added test)
eol_check: clean
```

### Research evidence

| Question | Source | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| Fields and keyword limits of structured output | `claude-api` skill reference, docs cache dated 2026-11-03 (not re-fetched from the web) | summary (skill text) | `output_config.format {type: json_schema, schema}` with `output_config.effort`; numeric, string-length and array-size keywords unsupported, `additionalProperties: false` required; the PM probe re-checks | documented |
| Haiku 5.5 thinking rules | `claude-api` skill reference, same cache | summary | `budget_tokens` returns 400; `thinking: disabled` only at effort `high` or lower | documented |
| Auth header | `claude-api` skill reference, same cache | summary | API keys use `x-api-key`; `Authorization: Bearer` is for OAuth tokens | documented |
| Does the Messages API have a seed field | `model_mapping.SAMPLING` (`anthropic` has only `stop`) plus the same skill docs | raw (code) | no seed parameter | source-verified |
| How the budget counts | `model_client.py:188-189`, `model_resources.py:21`, `model_transport.py:60-76` | raw | per run, per HTTP attempt | source-verified |
| Is a stripped schema a safe superset | `test_property_stripped_copy_accepts_every_instance_the_full_schema_accepts` (Hypothesis 6.170.15) | experiment | holds for positive applicators; non-monotone ones are refused | experiment |

### Configuration audit

| Value or behaviour | Config path | Validation | Example | Override and refusal tests | Class |
|---|---|---|---|---|---|
| request budget | `vlm.backends.<b>.call_budget` | `config.py` (int >= 0 or sentinel) | `ask_user`, comment in `configs/enhancer.example.yaml` | `test_budget_override_3000_with_stop_loads`, `test_budget_refusals` | configurable |
| budget stop | `vlm.backends.<b>.on_budget_exhausted` | `config.py` (two values) | same | `test_stop_ends_the_run_with_budget_exhausted_and_never_falls_back`, `test_budget_refusals` | configurable |
| structured output | `vlm.roles.<r>.output.format`, `schema_id` | `config._validate_anthropic_schemas` | commented Haiku role snippet | `test_a_role_bound_to_anthropic_may_send_json_schema_when_the_schema_passes`, `..._cannot_take_is_refused_at_load`, `test_json_object_stays_refused...` | configurable |
| thinking and effort | `vlm.roles.<r>.reasoning.*` | `anthropic_reasoning_refusal` at load and in `cloud_request` | snippet: `auto`, `medium` | `test_haiku_accepted_settings_load`, `test_haiku_400_settings_are_refused_at_load` | configurable; table protocol-bound |
| seed and attempts | `sampling.seed`, `reply_attempts`, `image_attempts` | `attempt_seed_refusal` | snippet: `seed: none` | `test_load_time_seed_requirement_follows_the_reachable_protocols`, retry tests | configurable |
| unsent keywords, model table, `seed_field` marker, `x-api-key` | module constants | none (no profile key) | none | strip tests, `test_the_rule_table_is_keyed_by_model_id_and_names_haiku`, key test | protocol-bound |
| `unsupported_param_policy` | profile key | not read anywhere in `src/` | n/a | n/a | defect, pre-existing, not touched |

## Noticed, not done

- Gemini structured output has the same gap (`cloud_request` never sends a schema); its fields are not in my evidence. Still refused at load.
- The role schema (`vlm_role.v0`) accepts `none` only for `seed` and `stop`. PM answer 4 (`none` for temperature, top_p, top_k) needs a change to `vlm_role.v0` and `enhancer_config.v0`; the snippet uses numbers (warned, not sent) plus `seed: none`.
- Writer output schema from the admin manifest (T017-FIX3), envelope states, prompt text and the `runs/` pilot profile: out of scope, untouched.
- `model_client.py` edits beyond line 450 for the FIX3 merge: cloud-branch hash lines (about 892-919), `_retry_plain_image` (seed, marker, refusal), `_safe_attempts` whitelist, the import line and `MODEL_CALL_REVISION`. Pin-row conflict with T017-FIX3 is the PM's to resolve.
- Unverified until the live probe: `$id`, `$schema`, `pattern`, type arrays, `$comment` in a sent schema. If the API refuses one, shorten or extend `ANTHROPIC_UNSUPPORTED_KEYWORDS`.
- `tasks/active/T-009-model-client.md` quotes `Authorization: Bearer` (lines about 217, 477, 597, 1442); closed work, left unedited. No OAuth mode added; one would be needed only for OAuth tokens.
- A budget fallback from Anthropic to the local backend (`fallback_to_default_backend`) retries unseeded and is recorded as such; whether a local retry should be seeded is a PM question.
- The budget is per run object, so 3000 per run: the PM tracks the total across K2, K3, K4, kb_sync and pipeline runs.
