# CLOUD-2: planner reply transport on Anthropic without the grammar compile limit

## Task brief

- **Task:** CLOUD-2 (phase 1 Step 0, phase 2 code, phase 3 hand-in, fix round 1). **Role:** implementer (Claude Code worker, Sonnet 5.5). **Base:** `c3e61d8`. **R-IDs:** R-018, R-015, R-001, R-004, R-009, R-026 (decision 170: none weakened).
- **Haiku requests:** 12 of 12 allowed, all in phase 1 (table below); none since. The key was loaded at runtime by project Python; `.secrets/` was never opened. Evidence: `.tmp/CLOUD-2/results.json` (kept for the PM).
- **PM answers in force (pending user review):** `tool_forced` is the only new `output.transport` value; one bump `model-call-8` with a stage_guard pin; planner on Anthropic only; one forced-tool probe for a `tool_forced` role and the real schema compiled once at start for every Anthropic `response_format` json_schema role; no live call. Fix round 1 adds: a GUARDED schema file never changes; the value lives in version 3 only.
- **Status:** code and tests done; PM live check (T-017 H1 rerun) not run.

## Step 0 evidence (kept short; the full Step 0 was approved)

Problem: HTTP 400 "The compiled grammar is too large" for the planner schema as `output_config.format` on Haiku 5.5. Docs (platform.claude.com structured-outputs, "Schema complexity limits"): "limits apply to all requests with `output_config.format` or `strict: true`"; "Non-strict tools don't count toward this limit". Forced tool: define-tools "Forcing tool use" (Haiku 5.5 accepts it, "a forced call skips thinking"; manual extended thinking is refused); prefill is refused on 4.6+.

| # | Request (`claude-haiku-5-5`, `thinking: disabled`, bodies from the H1 planner requests) | HTTP | Valid vs registered schema |
|---|---|---|---|
| 1-4 | prompt_only (no format, schema text in system) x4 | 200 | 3 of 4 (row 2 `ValidationError`) |
| 5-8 | non-strict forced tool, stripped schema x4 | 200 | 4 of 4 |
| 9 | real schema as `output_config.format`, `max_tokens` 16 | **400** | `req_011ChDuhseY73wo7whyXnMVi` |
| 10 | same tool with `strict: true` | **400** | `req_011ChDuhuKzy6erWmTpEWKa9` |
| 11 | prefill `{` | **400** | `req_011ChDuhw5RDcrtrv2dRGRkQ` |
| 12 | forced tool with the **unstripped** registered schema | 200 | yes |

The limit belongs to the grammar pipeline; a non-strict tool is outside it. 4 and 5 requests prove the mechanism, not a rate (assumption).

## Completion report

### Behaviour

- **`output.transport: tool_forced`** (planner, Anthropic, configuration version 3): `cloud_request` sends one non-strict tool `submit_reply` with an unstripped copy of the per-request schema, `tool_choice {type: tool, name}`, no `output_config.format`; `system` and `messages` are byte-equal to the `response_format` body (R-004, R-001). `response_format` and local bodies are unchanged (pinned by tests).
- **Reply:** only the single `tool_use` block's `input` is the reply; text beside it is ignored (R-026; deviation from Step 0, which refused it). No block, several, another name, or a non-object input is `SCHEMA_INVALID` with `tool_reply_check` = `TOOL_USE_MISSING` / `TOOL_USE_MULTIPLE` / `TOOL_NAME_MISMATCH` / `TOOL_INPUT_NOT_OBJECT`, then the one retry. `max_tokens` and `refusal` are `INCOMPLETE_OR_REFUSED`, never parsed. The response body is parsed with `strict_json` (duplicate names, nonfinite numbers, depth), then validated against the full per-request schema; `sent_schema_hash` names `tools[0].input_schema`.
- **Config:** refused at load for every role but the planner, every provider but Anthropic, `format` other than `json_schema`, `validate_response: false`, `reasoning.mode: on`, `extra_body` with `tools`/`tool_choice`, a backend with `on_budget_exhausted: fallback_to_default_backend`, an image planner on a backend with `allow_images: false`, and any configuration below version 3.
- **Probes:** a `tool_forced` role sends one forced-tool request with its real schema at `max_tokens` 16 (200 passes whatever the `stop_reason`); every Anthropic `response_format` json_schema role gets one extra `schema` challenge. HTTP 400 on it is `PROBE_FAILED` and the failure names `vlm.roles.<role>.output.transport`; another outcome keeps its own code. Local backends send neither.

### Commands run (`.venv` python, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH` = worktree `src`; each file its own process through `tools/agent-workflow/test_slot.py -- python -m pytest -q -p no:cacheprovider <file> --basetemp=.tmp/CLOUD-2/bt_<file>`; final state of the tree unless marked)

| File | Result line |
|---|---|
| `test_cloud2_tool_forced` | `79 passed in 17.21s` |
| `test_ms2_config_v2` / `test_ms2_manifest_v1` | `115 passed in 11.54s` / `97 passed in 27.72s` |
| `test_cloud1_anthropic` / `test_stage_revisions` | `133 passed in 45.43s` / `441 passed in 172.76s` |
| `test_real_contracts` / `test_loader` / `test_model_config` / `test_validation_config` | `3 passed in 1.59s` / `135 passed in 46.00s` / `123 passed in 64.63s` / `169 passed in 43.93s` |
| `test_embed_server_manifest` / `test_model_client_image_side` / `test_multi_source_contracts` | `35 passed, 1 skipped in 10.83s` / `36 passed in 46.39s` / `97 passed in 0.96s` |
| `test_profiles` / `test_model_schemas` / `test_output_schema` | `196 passed, 2 skipped in 170.99s` / `14 passed in 3.02s` / `16 passed in 22.63s` |
| `test_model_production_probes` / `test_model_probe` / `test_probe_request_settings` | `84 passed in 65.60s` / `5 passed in 0.22s` / `132 passed in 70.90s` (the first run had `1 failed, 131 passed`: see Fix round 1, item P3.1) |
| `test_planner_config` / `test_cloud1_planner` / `test_probe_retry` / `test_k2_probe` | `26 passed in 3.82s` / `7 passed in 13.27s` / `34 passed in 26.81s` / `18 passed in 1.35s` |
| `test_kb_sync_production_probes` / `test_planner` / `test_model_client` / `test_planner_generate_schema` / `test_model_cloud` | `1 passed in 11.48s` / `67 passed in 38.15s` / `79 passed in 100.17s` / `18 passed in 51.82s` / `10 passed in 0.12s` |
| `test_k1_config` / `test_embed_server_config` | `558 passed in 225.59s` / `160 passed in 48.35s` |
| `test_field_contracts` (phase 3, before fix round 1 touched `config.py` again) | `529 passed, 55 skipped in 984.85s`; **not re-run in fix round 1** |

Digest (`.tmp/CLOUD-2/digests.py`): only `model_call` moved: `model-call-8` re-pinned to `414a6e4e59bafc3ada60b8736d8c7af68ae59ce2b6a1714669d4316a306d4b5e` (was `1106ec76...`; `_call` changed in fix round 1); every other stage `same`; `unclassified [] [] []`.

### Mutants (scratch copy of `src`, `tests/test_cloud2_tool_forced.py -x`)

Phases 2 and 3: 16 mutants on the tool request, reply parser, stop reasons, hash source, fallback refusal, roles/provider refusals, probe cases, arithmetic; all killed (one, the image-probe one-request cap, survived first and got its own test). Fix round 1 (8, all killed; each run `1 failed` on the intended test): (1) `strict_json(response.text) if tool_forced else ...` -> `response.json()` (the reviewer's m1 family: depth, duplicate key, NaN); (2) hint on `http_status == 400` -> any status; (3) `statuses[name] = error.status` -> `400`; (4) version gate `if ...tool_forced` -> `False`; (5) `_planner_role_schema_id` -> v0; (6) composition `name == 'planner'` -> `'writer'`; (7) budget-fallback refusal off; (8) `allow_images is False` -> `is None`.

### `not_run`

Live Haiku requests; a full `profile check` of an Anthropic `tool_forced` profile (no such tracked profile; `runs/` is the PM's); `ruff` (not installed in `.venv`); `test_field_contracts` after fix round 1 (it reads no changed key; the PM may run it); the stage digests were recomputed once after the last pinned-function edit.

### Changed files (`git status --short`, fix round 1 included)

`configs/enhancer.example.yaml`, `contracts/README.md`, `contracts/schemas/enhancer_config.v0.schema.json` and `vlm_role.v0.schema.json` (net **no diff** against `c3e61d8`), new `contracts/schemas/vlm_role.v1.schema.json`, `contracts/vlm-request.md`, `docs/rules/VLM_CALL_RULES.md`, `src/prompt_enhancer/config.py`, `model_client.py`, `model_cloud.py`, `model_probe.py`, `tests/stage_guard.py`, `tests/test_stage_revisions.py`, new `tests/test_cloud2_tool_forced.py`, this file.

### Open items

- U6: reply quality of the tool path over the pilot is unmeasured (5 of 5 valid in the sample); the one retry is the safety net.
- `PLANNER_CALL_FAILED` keeps only a cause code, so the `output.transport` hint may not reach the user through the planner.
- The start probe registers the configured `planner_reply.v0`; the generate variant is assumed grammar-equivalent (assumption).
- Profiles under `runs/` need `schema_version: 3`, `transport: tool_forced` and a `probe.max_requests` covering the extra request: the PM's.
- `profiles.py` hashes `config.py` (`check_code_sha256`): a confirmed profile's `activation.json` goes stale and `profile check` runs again (the check code changed).

## Configuration audit (row by row; `T::` = `tests/test_cloud2_tool_forced.py`)

| Value | Validated config | Example that states it | Override test | Refusal test | Class |
|---|---|---|---|---|---|
| `vlm.roles.planner.output.transport: tool_forced` | role schema `contracts/schemas/vlm_role.v1.schema.json` (enum) used at `config.py:2110, 2933` through `_planner_role_schema_id` (`config.py:2070`); composed into the planner role of version 3 at `config.py:2286-2290`; planner gate `config.py:2128`; `_validate_tool_forced` `config.py:1155-1186` | `configs/enhancer.example.yaml:304` (comment; the value there is `response_format`) | `T::test_a_version_3_planner_may_name_tool_forced_and_every_other_role_may_not`, `T::test_the_version_3_composition_allows_tool_forced_for_the_planner_only`, `T::test_the_planner_reads_its_reply_from_the_forced_tool_and_keeps_the_invariants` | `T::test_only_the_planner_on_anthropic_may_select_tool_forced` (other roles, providers, formats, `validate_response`, reasoning on), `T::test_a_loaded_config_refuses_tool_forced_on_a_local_backend_and_on_another_role`, `T::test_request_refusals` | configurable |
| Configuration version gate | `_require_tool_forced_version` `config.py:2077`, called at `config.py:2882` for every command | n/a | `T::test_a_version_3_planner_may_name_tool_forced_and_every_other_role_may_not` | `T::test_a_configuration_below_version_3_that_names_tool_forced_is_refused_with_the_key_and_the_version` (versions 1 and 2, two commands) | protocol-bound (versioning rule) |
| `TOOL_FORCED_ROLES = {planner}` | `config.py:1152` | n/a | n/a | the refusal tests above | artifact-bound |
| Fallback routes refused: budget fallback, image role with `allow_images: false` | `config.py:1177-1186` (keys `vlm.backends.<b>.on_budget_exhausted`, `.allow_images`); runtime guard `model_client.py:861` | existing keys, example `configs/enhancer.example.yaml` backends | `T::test_tool_forced_refuses_the_routes_that_fall_back_to_the_local_default_backend` (stop, `allow_images: true`, sentinel pass) | same test (both refused, key named); mutants 7, 8 | configurable (existing keys) |
| Tool name `submit_reply`, description | `model_cloud.py:136-137`, pinned in `tests/stage_guard.py` | n/a (identifier read back) | `T::test_request_forces_one_non_strict_tool_with_the_unstripped_schema_and_no_format` | `T::test_the_reply_parser_refuses_a_wrong_structure_with_a_closed_code` (name mismatch) | protocol-bound |
| `tool_choice` shape, no `output_config.format`, unstripped schema | `model_cloud.py:193-202, 228` | n/a | same, `T::test_the_request_schema_is_a_copy` | `T::test_request_needs_an_object_schema` | protocol-bound |
| Accepted `stop_reason` set for a tool call | `model_client.py:1030` | n/a | `T::test_a_valid_tool_reply_returns_the_input_and_names_the_bytes_sent` | `T::test_a_truncated_or_refused_tool_call_is_never_parsed` | protocol-bound |
| Reply codes `TOOL_USE_*`, `TOOL_NAME_MISMATCH`, `TOOL_INPUT_NOT_OBJECT`; strict body parse | `model_cloud.py:140-163`; `model_client.py:1017, 1047` | n/a | `T::test_property_any_json_object_in_the_one_tool_block_parses_back_to_itself` | `T::test_a_reply_without_exactly_one_valid_tool_block_takes_the_invalid_reply_path`, `T::test_a_duplicate_key_or_a_nonfinite_number_in_a_tool_input_is_refused`, `T::test_a_tool_input_nested_past_the_depth_bound_is_refused_and_a_shallow_one_is_not` | protocol-bound |
| Client-side schema validation (`validate_response: true` required) | `config.py:1167-1168`; `model_client.py` validation after the parse | existing example line | `T::test_the_per_request_bound_is_checked_client_side_on_the_tool_input` | `T::test_a_tool_input_that_breaks_the_full_schema_is_refused_without_its_content` | configurable (existing key) |
| Probe compile `max_tokens` 16 | `model_probe.py:276-282` | n/a | `T::test_a_tool_forced_role_probes_once_with_its_real_schema_as_a_forced_tool`, `T::test_a_response_format_role_also_posts_its_real_schema_once_at_start` | `T::test_a_refused_real_schema_stops_the_role_at_start_naming_the_transport_key` | safety-bound (the 400 was seen at 16) |
| Probe HTTP status carried for the `schema` challenge; hint only on 400 | `model_probe.py:36, 377-380, 433, 453`; `model_client.py:884` | n/a | `T::test_the_transport_hint_is_given_only_for_an_http_400_on_the_real_schema` | same (500, 429 and an unfinished challenge give no hint); mutants 2, 3 | protocol-bound (closed integer, never the body) |
| `probe.max_requests` floor (existing key; `real`, `forced`, `structured_images`; message names the needed number) | `config.py:1015-1024` | existing `probe` blocks (sentinel `design_required` in the tracked examples) | `T::test_the_probe_request_arithmetic_counts_the_real_schema_request` (exact floor loads, both transports) | same (floor minus one refused, message `raise it to at least N`) | configurable (existing) |

No hidden default (`transport` stays required by the role schema). No retained literal is a `defect` or unclassified.

## Pre-mortem: failure class to covering test (`T::` = `tests/test_cloud2_tool_forced.py`)

| Failure class | Covering test |
|---|---|
| Reply wrapped in prose or a fence | `T::test_the_reply_parser_refuses_a_wrong_structure_with_a_closed_code`, `T::test_text_beside_the_tool_block_is_not_the_reply`, `T::test_property_any_json_object_in_the_one_tool_block_parses_back_to_itself`, `T::test_property_the_number_and_position_of_tool_blocks_decide_the_reply` |
| Truncated or refused reply | `T::test_a_truncated_or_refused_tool_call_is_never_parsed`, `T::test_the_planner_reports_a_failed_tool_call_with_its_cause` |
| Input not an object; zero, two or misnamed tool blocks | `T::test_a_reply_without_exactly_one_valid_tool_block_takes_the_invalid_reply_path`, `T::test_property_a_non_object_input_never_parses` |
| Input breaks the schema, the bound, the depth, a duplicate key or NaN | `T::test_a_tool_input_that_breaks_the_full_schema_is_refused_without_its_content`, `T::test_the_per_request_bound_is_checked_client_side_on_the_tool_input`, `T::test_a_tool_input_nested_past_the_depth_bound_is_refused_and_a_shallow_one_is_not`, `T::test_a_duplicate_key_or_a_nonfinite_number_in_a_tool_input_is_refused` |
| A probe passing for a schema the real call refuses | `T::test_a_refused_real_schema_stops_the_role_at_start_naming_the_transport_key`, `T::test_a_refused_tool_schema_also_shows_at_start_as_a_failed_probe`, `T::test_a_tool_forced_role_probes_once_with_its_real_schema_as_a_forced_tool` (residual: the probe uses the registered schema, an upper bound; assumption) |
| A wrong hint on another probe failure | `T::test_the_transport_hint_is_given_only_for_an_http_400_on_the_real_schema` |
| Retry re-sending a refused schema | `T::test_a_refused_schema_400_is_a_single_http_error_never_resent`; `T::test_the_planner_reads_its_reply_from_the_forced_tool_and_keeps_the_invariants` (R-001 `NOT_VERBATIM` retried, same body) |
| A local or `response_format` byte change | `T::test_response_format_bodies_are_unchanged_by_the_new_transport`, `T::test_local_probe_bodies_do_not_change_with_a_real_schema`, `test_model_client` (79 passed) |
| Selected where not implemented, or after a fallback | `T::test_only_the_planner_on_anthropic_may_select_tool_forced`, `T::test_a_loaded_config_refuses_tool_forced_on_a_local_backend_and_on_another_role`, `T::test_tool_forced_refuses_the_routes_that_fall_back_to_the_local_default_backend`, `T::test_a_tool_forced_role_never_runs_on_another_provider` |
| A guarded schema edited in place; a lower version using the value | `tests/test_ms2_config_v2.py::test_existing_schema_files_are_never_edited_in_place`, `T::test_only_the_newest_role_schema_lists_the_new_value_and_v0_is_untouched`, `T::test_a_configuration_below_version_3_that_names_tool_forced_is_refused_with_the_key_and_the_version` |
| `extra_body` replacing `tools`/`tool_choice`; R-026 injected text | `T::test_request_refusals`; `T::test_text_beside_the_tool_block_is_not_the_reply` |
| Probe allowance too small | `T::test_the_probe_request_arithmetic_counts_the_real_schema_request`, `T::test_the_probe_allowance_pays_for_the_extra_challenge_or_leaves_the_proof_incomplete` |

## Draft for implementation-notes

- **Built (CLOUD-2):** `output.transport: tool_forced` (planner, Anthropic, configuration version 3): one non-strict forced tool `submit_reply` with the unstripped per-request schema, reply read from the single `tool_use` input after a strict body parse and validated client-side; plus a start probe that posts every Anthropic `json_schema` role's real schema once.
- **Decided (PM, pending user review):** only the planner, only Anthropic, others refused at load naming the key; one `model-call-8` bump; a guarded schema never changes, so the value is added through `vlm_role.v1` and the `enhancer_config.v2` composition, and a file below version 3 is refused; the planner cannot select `tool_forced` when a budget or image fallback would reach the local backend; text beside the tool block is ignored (R-026).
- **Rejected:** `prompt_only` (3 of 4 live replies schema-valid); prefill (HTTP 400, `req_011ChDuhw5RDcrtrv2dRGRkQ`); `strict: true` tool (same 400, `req_011ChDuhuKzy6erWmTpEWKa9`); stripping the schema for the tool (the unstripped one gave 200); editing `vlm_role.v0` or `enhancer_config.v0` in place (the MS-2 guard).
- **Evidence:** 12 live Haiku requests (table above): forced non-strict tool 5 of 5 HTTP 200 and schema-valid; the real schema as `output_config.format` at `max_tokens` 16 gave 400.
- **Revisions moved:** `MODEL_CALL_REVISION` `model-call-7` -> `model-call-8` (digest `414a6e4e59bafc3ada60b8736d8c7af68ae59ce2b6a1714669d4316a306d4b5e`); only the `model_call` digest moved. By the `model-call-7` precedent the stages that fold in `model_call` (K2 naming/observe, K3, K4) recompute at the next `kb sync`; K1 is unaffected (not re-verified here).
- **Server impact:** none (no server, GPU or model file). The pilot profile needs `schema_version: 3`, `transport: tool_forced`, `on_budget_exhausted: stop`, `allow_images: true` for the image planner and a `probe.max_requests` that pays one more request per Anthropic `response_format` json_schema role (the PM sets it).
- **Open:** live tool-path quality (U6); the H1 rerun; `PLANNER_CALL_FAILED` does not carry the transport hint.

## Fix round 1 (QA feedback: Gemini PASS, Opus FAIL on one P1)

| Finding | Fix (file:line) | Test |
|---|---|---|
| **P1** `vlm_role.v0` and `enhancer_config.v0` edited in place (MS-2 guard fails) | both restored: `git diff c3e61d8 -- contracts/schemas` is empty for them. New `contracts/schemas/vlm_role.v1.schema.json` (v0 plus the value). Version 3 only: planner role fragment composed from it `config.py:2286-2290`, role checks `config.py:2110, 2933`; version gate `config.py:2077, 2882` ("tool_forced needs schema_version 3 (enhancer_config.v2)"); wording in `contracts/README.md` (VLM roles line), `contracts/vlm-request.md`, `VLM_CALL_RULES.md` | `tests/test_ms2_config_v2.py` (115 passed, includes the guard), `T::test_only_the_newest_role_schema_lists_the_new_value_and_v0_is_untouched`, `T::test_the_version_3_composition_allows_tool_forced_for_the_planner_only`, `T::test_a_configuration_below_version_3_...` |
| **P2** probe allowance | floor exact for both transports (`config.py:1015-1016`), message now ends `raise it to at least N` (`config.py:1024`). Checked: tracked examples `configs/enhancer.example.yaml:207`, `enhancer.dev.example.yaml:109,122,152` hold the sentinel `design_required` (refused by request commands until set; nothing to adjust); test profiles `tests/conftest.py:34` (1, local), `tests/profile_fixtures.py:45` (1, local), `tests/cli_fixtures.py:76` (12, local); the Anthropic test backends set their own `probe` in `test_cloud1_anthropic.py`, `test_cloud1_planner.py`, `test_cloud2_tool_forced.py` (all pass). `runs/` profiles are the PM's | `T::test_the_probe_request_arithmetic_counts_the_real_schema_request` |
| **P3.1** hint on any non-complete status | `ProbeHttpError` (`model_probe.py:36`), `http_status` on `ProbeResult` and, for the `schema` challenge only (existing event shapes stay byte-equal; `test_probe_pair_outcome_property` failed when I first added it to every case), `model_probe.py:377-380`; hint only for 400 `model_client.py:884` | `T::test_the_transport_hint_is_given_only_for_an_http_400_on_the_real_schema` (400 yes; 500, 429, unfinished no) |
| **P3.2** depth test did not isolate the check | schema-valid deep object (`d` takes any object); the redundant `check_json_depth(value)` call and import removed because the body parse (`model_client.py:1017`) already enforces depth, so m1 is now the strict-parse mutant | `T::test_a_tool_input_nested_past_the_depth_bound_is_refused_and_a_shallow_one_is_not`; mutant 1 |
| **P3.3** `response.json()` skipped strict checks | `payload = strict_json(response.text) if tool_forced else response.json()` `model_client.py:1017` | `T::test_a_duplicate_key_or_a_nonfinite_number_in_a_tool_input_is_refused` (duplicate, NaN, +/-Infinity, 1e999, plus a clean control) |
| **P3.4** fallback combos failed late | refused at load: `config.py:1177-1186` (budget fallback; image planner with `allow_images: false`), runtime guard `model_client.py:861` kept | `T::test_tool_forced_refuses_the_routes_that_fall_back_to_the_local_default_backend` |
| Property on block count and position | Hypothesis over 0..7 blocks, text around | `T::test_property_the_number_and_position_of_tool_blocks_decide_the_reply` |

**Class sweep (guards over `contracts/`, `prompts/`, `configs/`; found by grepping `tests/` for 64-hex literals, `sha256`, `GUARDED`, `stage_digest`, `PINS` next to those paths):** `test_ms2_config_v2` (GUARDED), `test_ms2_manifest_v1`, `test_multi_source_contracts`, `test_stage_revisions` (`stage_digest` pins), `test_embed_server_manifest`, `test_model_client_image_side`, `test_model_schemas`, `test_output_schema`, `test_profiles` (`schemas_sha256`), `test_real_contracts`, `test_loader`, `test_model_config`, `test_validation_config`, `test_model_production_probes` (probe bundle pins), `test_k1_config`, `test_embed_server_config`: all run, results in the table above. Root cause of the miss: the phase 3 test list did not include the MS-2 guard; the sweep is now in this file.

## Handoff

- Worktree `D:\Project_github\prompt-enhancer-pipeline\.tmp\worktrees\CLOUD-2`, base `c3e61d8`; nothing committed. The PM restages (the staged index predates fix round 1).
- Scratch: `.tmp/CLOUD-2/` keeps `results.json` only.
