# T017-FIX3: the writer sends the admin output schema (G1); golden retrieval switch; S1 code, `not_reached` and `degraded` rules

Kind: code. R-IDs: R-001, R-004, R-009, R-013, R-015, R-020, R-026, R-027. Decisions 194, 197, 198, 199, 200 (user, 2026-11-20). Approach note: `.tmp/T017-FIX3/rca3-report.md`. Evidence labels: **src** source-verified, **doc** documented, **exp** test run, **asm** assumption.

## Step 0

### Why J4a5's writer request carried no schema (G1, src)

1. The pilot manifest has `output_schema_id: none` (rca3 G1, "The writer manifest has `output_schema_id: none`"; T-017 phase 4b-1 hashes unchanged). `resolve_writer_format` (`config.py:2618-2627`) maps `none` to `format: none`; `model_client.py:882-883,903-904` put a schema in the body only when the resolved format is `json_schema`. So nothing was sent, as designed.
2. Even an admin who has a schema could not name one: a schema id resolves only under `contracts/schemas/` (`config.py:437-444` `_schema(root, id)`; `model_client.py:192` registry of `contracts/schemas`; `writer.py:124`; `validation.py:121-123` uses the S5 registry of the same folder). `contracts/` is the project's, not the admin bundle's, so the admin route of decision 194 has no place for the file. The drafter always writes `none` (`prompts/manifest_drafter_v0.txt:8`).
3. When a contract id *is* named, the path already works end to end: resolve → `json_schema` → `response_format` to llama.cpp → `self.schemas.validate` client-side (`model_client.py:998-999`) → writer re-check (`writer.py:203-205`) → S5 contract check on every repair (`validation.py:121-123`). Tests: `tests/test_writer.py:101`, `test_validation.py:172` (contract id `verifier_result.v0`).

So the gap is (2): where an admin bundle's schema lives and how it is registered.

### Class sweep (before any fix)

Readers of the manifest's output settings (`output_schema_id`, writer `from_profile_manifest`): `config.py:1101-1106` (role/sentinel rule), `config.py:2022-2023` (writer paired refs), `config.py:2611-2628` `resolve_writer_format`; `model_client.py:348-349` `_proof_key`, `:486-488` production probe, `:747-748` `concrete`, `:765-768` writer resolve, `:875-883`/`:998-1001` schema send and validate (CLOUD-1 region, not edited); `writer.py:143-146,203-205`; `validation.py:121-123`; `profile_validators.py:126` `output_format`; `verifier_policy.py:68`; `profiles.py:699-701` (golden structural check via `output_format`).

Readers and writers of `retrieval.state`: `envelope.py:25-31` (writer), `:39` (status degraded), `:79` (trace column); `intake.py:152,185,198,246` (intake's own enum `disabled|pinned|failed`, and the refusal envelope `:155-159`); `storage/tables.py:72,129-134` (trace and eval columns `Ref(response_envelope.v0 …)`); `validation.py:248` (assembly state, not envelope); `assembly.py:262-363` (S3's own enum); `eval_run.py:43,337,423` (envelope schema and snapshot id only); `profiles.py:61,692-702` (golden judging); `planner.py:351-367,462-472` (`RETRIEVAL_FAILED` warning). Schemas: `response_envelope.v0` (enum), `intake_result.v0:49` (`$ref` envelope), `golden_case.v0`. Docs: `S6-envelope.md`, `S3-knowledge.md`, `P-profiles.md`, `architecture.md`, `contracts/README.md`, `run-guide.md`.

### Unknowns

- Whether the per-request writer client can carry a bundle schema without editing CLOUD-1's lines 875-911. Answer (src): the owner client is composed per request (`pipeline.py:290`), so a request-scoped registry (contracts + the bundle schema) set on that client reaches `self.schemas` at 349, 879-883 and 998-1001 with no edit there.
- Whether llama.cpp b11921 enforces every keyword of an admin schema (rca3 not_run). Not testable on CPU; client-side validation stays the guard.

### Questions (ranked, each with my proposed answer)

1. **Where does an admin schema live?** Proposed: `<bundle>/output_schema.json` (artifact-bound name, next to `system_prompt.txt`). Its `$id` must equal `output_schema_id`. Loaded and checked at profile load (`profiles._view`, both check and resolve): strict JSON, Draft 2020-12 `check_schema`, `$id` match, no clash with a `contracts/schemas` id; bound into `activation.json` as `output_schema_sha256`, compared at resolve and at the end of check. Refusals: `output_schema_id` names an id found nowhere; the file is invalid; the file exists while `output_schema_id` is `none` (an ignored file is a hidden setting, AGENTS.md §5). `none` stays valid and sends nothing. A contract id under `contracts/schemas/` keeps working. `profile_manifest.v0` is unchanged (it is the drafter's output contract, and prompts are out of scope).
2. **Golden case contract.** Proposed: `golden_case.v1` (`schema_version: 2`). `request.retrieval: enabled | disabled` and `expect.grounding: required | not_required` are both required, so no case relies on a default. A v0 file (`schema_version: 1`) is refused at check with a message naming v1, not read with a guessed meaning. Alternative: read v0 as "retrieval from config, grounding not_required". Superseded `golden_case.v0` stays as a file.
3. **How a `disabled` case runs.** Proposed: as `eval_run.py:356-360` already does, a second `Pipeline` owns a deep copy of the config with `rag.enabled: false`. It is built only when the check has a `disabled` case and the deployment has retrieval on. `GoldenAdapter` picks the pipeline per case, so the switch never leaks into another case. Its identity becomes `s0-s6.v3`. An `enabled` case under a deployment with `rag.enabled: false` is refused before any case runs (`PROFILE_GOLDEN_INVALID`).
4. **Grounding rule (decision 199).** Proposed: `grounding: required` requires `retrieval: enabled`, and `expect.status` may hold only `success` or `repaired`; the schema refuses `degraded` there. At judging, such a case also needs `envelope.retrieval.state == retrieved`. Failure codes go in the result entry's `codes`: `GOLDEN_GROUNDING_DEGRADED` (envelope status `degraded`), `GOLDEN_NOT_GROUNDED` (state is not `retrieved`), `GOLDEN_RETRIEVAL_NOT_DISABLED` (a `disabled` case whose envelope state is not `disabled`), and `GOLDEN_STATUS_UNEXPECTED`.
5. **S1 code and `not_reached` (decisions 198, 200).** Proposed: `response_envelope.v1` adds a required field `retrieval.error`, which is null or a closed code (`WINDOW_BUDGET_EXCEEDED`, `SNAPSHOT_MISSING`, `RETRIEVAL_FAILED`, `KNOWLEDGE_READ_FAILED`, `KNOWLEDGE_FAILED`). The schema ties `error != null` to `state == failed`. It is a field, not a warning: the warning list is envelope-wide and binds a code to no stage, whereas the field states why retrieval failed. A closed enum carries no prompt or KB text (R-026). The state enum gains `not_reached`. With rag on, the envelope reports:
   - `failed` with the code when S1 failed, when S1 started and did not return, when the planner reported `RETRIEVAL_FAILED` (code `RETRIEVAL_FAILED`), when S3 failed (its code), or when intake refused with `KNOWLEDGE_FAILED`;
   - otherwise, with no assembly, `not_reached`: an intake refusal for another reason, a plan with no intent, clarification, or a fatal model error after S1 was ready.

   `disabled` is unchanged. Bumps: `response_envelope.v1`; `intake_result.v1`, whose refusal envelope `$ref` changes; the trace and eval columns move to v1. The v1 enum is a superset of v0, so old rows stay valid. v0 files stay.

### Plan (finish check in brackets)

1. Bundle schema: loader, `ProfileView.output_schema`, activation binding, `resolve_writer_format(profile_schema_id=)`, request-scoped registry on the writer client, writer and S5 use the same registry. [tests: body carries the bundle schema; reply validated against those bytes on draft and every repair; `none` sends none; each refusal; system bytes and order unchanged]
2. `golden_case.v1`, two pipelines, judging codes. [tests: `disabled` case opens no store and makes no S1, S3 or K call, while other cases keep retrieval on; v0 refused; grounding rules]
3. `response_envelope.v1` and `intake_result.v1`: `error`, `not_reached`, every consumer, docs. [unit and property tests over generated outcomes]
4. Focused test sets through `test_slot.py`, 3 to 10 mutants, pre-mortem table, Configuration audit, Completion report.

**Finish check:** the brief's Done check. The listed test files are green through `test_slot.py`, the mutants are reported, and this file holds the Completion report and the audit.

### Configuration impact (before the first runtime edit)

No `enhancer_config` key is added or changed. The new inputs are admin and contract data, not deployment values:

| Item | Kind | Validation | Tests |
|---|---|---|---|
| `<bundle>/output_schema.json` | artifact-bound file name | strict JSON, Draft 2020-12, `$id == output_schema_id`, no contract clash, refused when `none` | override (a named bundle schema reaches the body) and refusals (missing, invalid, mismatched id, clash, present with `none`) |
| `golden_case.v1` `request.retrieval`, `expect.grounding` | contract fields (protocol-bound enums) | JSON Schema plus the check's run-time refusal of `enabled` under rag off | non-default (`disabled`) and refusals (missing field, v0 file, `enabled` under rag off, grounding with `degraded`) |
| `response_envelope.v1` `retrieval.error`, `not_reached` | contract | schema `if/then` | unit and property tests |
| Golden failure codes, `s0-s6.v3` | protocol-bound or artifact-bound literals | — | judging tests |

## Steps

Phase 1 approved by the PM on 2026-11-20 (answers 1 to 5 = the proposals). This task owns `response_envelope.v1`, `intake_result.v1` and `golden_case.v1`.

1. Schemas: added `response_envelope.v1` (`retrieval.error` closed enum, required; the state `not_reached`; `if state failed then error string else null`), `intake_result.v1` (`$ref` v1) and `golden_case.v1` (`schema_version 2`, required `request.retrieval` and `expect.grounding`; `grounding: required` forces `retrieval: enabled` and a status of `success`/`repaired` only). The v0 files are unchanged.
   Code: in `envelope.py`, `_retrieval` and `intake_retrieval` are the one rule; `intake.py` builds its refusal envelope through `intake_retrieval`. `tables.py` `ENV`, `eval_run.py` `ENVELOPE_SCHEMA` and `profiles.py` `ENVELOPE_SCHEMA`/`GOLDEN_SCHEMA` now point at v1.
2. Bundle schema: `profiles.load_output_schema` (`OUTPUT_SCHEMA_FILE = output_schema.json`, code `PROFILE_OUTPUT_SCHEMA_INVALID`) runs in `_view`. `ProfileView.output_schema`/`output_schema_id` hold it. The activation gains `output_schema_sha256`, which the end-of-check comparison and resolve both check. `SchemaRegistry.with_schema` and `with_output_schema` are in `model_schemas.py`.
   `resolve_writer_format(profile_schema_id=)` is in `config.py`. `ModelClientRun.use_output_schema`/`BoundModelClient.use_output_schema` set the request-scoped registry on `RoleClient.schemas`. `RoleClient.output_schema_id` is passed at `model_client.py` writer resolve; lines 875-911 are not edited. The pipeline binds it after compose; `writer.py`, `validation.py` and `verifier_policy.py` use the same registry or id.
3. Golden: `_golden_cases(..., rag_enabled)` refuses a v0 file by name and refuses an `enabled` case under rag off. `golden_codes` returns the four codes into the entry's `codes`. `GoldenAdapter` is `s0-s6.v3`, an async context manager with a lazy retrieval-off `Pipeline`, and selects the pipeline per case. `cli_commands.profile_check` owns both.
4. Tests: v0-shaped fixtures and doubles moved to v1 (`error` key, `retrieval` key, runner `s0-s6.v3`). New: `tests/test_output_schema.py` (A), `tests/test_golden_retrieval.py` (B, C end to end, D), two bundle-binding tests in `test_profiles.py`, envelope property and closed-code tests in `test_envelope.py`, refusal-envelope tests in `test_intake.py`.
   Docs: S0, S4, S6, P, architecture, run-guide, contracts/README. Then the focused sets via `test_slot.py`, 10 mutants and 3 baselines on a scratch copy of `src`, the pre-mortem and the audit.

## Pre-mortem (decision 169)

| Way this could be wrong | Guard | Evidence |
|---|---|---|
| R-004: the schema changes system bytes or message order | the schema goes only into `response_format`; messages are built as before | `test_writer_sends_bundle_schema_and_keeps_system_bytes` (messages equal to the `none` baseline; bytes `Admin\r\n`) |
| `none` sends a schema, or is refused | `load_output_schema` returns None for `none` without a file; nothing is bound | `test_none_without_file_sends_nothing...`, `'response_format' not in baseline`, `ragon.bound == []` |
| Schema sent but not validated, or validated against other bytes | one parsed schema object goes into the request-scoped registry that both builds the body and validates the reply; S5 builds its registry from the same `profile.output_schema` | `test_writer_validates_reply_against_bundle_schema` (3 invalid replies give `SCHEMA_INVALID`); mutants m7, m8 |
| S5 repair loses the schema | S5's contract check and the repair writer use the same bound registry | `test_s5_repair_is_checked_against_the_bundle_schema`; mutant m7 |
| Retrieval switch leaks, or a ragoff case opens the store | a separate `Pipeline` on a deep copy with `rag.enabled: false`; per-case selection | `test_disabled_case_opens_no_store_while_enabled_case_still_retrieves` (every KB entry point recorded: `[]` for the disabled case, `[('store', True)]` with the enabled one); mutant m5 |
| The S1 code carries text (R-026) | `retrieval.error` is a closed schema enum; the code is read from `error.code`, never `message` | `test_retrieval_error_is_a_closed_code_never_stage_text`; mutant m10 |
| `not_reached` hides a real failure | every failure branch is checked before the `not_reached` fallback; SNAPSHOT_MISSING and KNOWLEDGE_FAILED stay `failed` | property `test_retrieval_state_is_one_enum_value_and_not_reached_only_without_s3`, `test_refusal_envelope_with_retrieval_on_is_failed_only_at_the_knowledge_boundary`; mutants m1, m2 |
| `degraded` passes a grounding case | the schema refuses it in `expect.status`, and `golden_codes` adds `GOLDEN_GROUNDING_DEGRADED` even for a hand-edited case | `test_golden_codes`, `test_case_schema_refuses_degraded_on_a_grounding_case`; mutant m3 |
| Old envelopes, traces or golden files read wrongly | v0 schema files are unchanged; the trace column enum is a superset; a v0 golden file is refused by name, never guessed | `test_retired_golden_v0_case_is_refused_by_name`; `test_trace_store.py` green |
| Prompt spans altered (R-001) | no prompt, brief or span code changed | the whole writer, validation and pipeline sets are green with unchanged span assertions |

## Completion report

**Behaviour.**
- (A) An admin bundle may hold `output_schema.json`, whose `$id` equals `output_schema_id`. Every profile load checks it, `activation.json` binds its bytes (`output_schema_sha256`), and the pipeline binds it per request to the composed client. Every writer attempt, S5 repairs included, then sends it as `response_format.json_schema` and validates the reply against the same parsed schema; S5's contract check uses it too. `none` sends nothing. Refusals: `PROFILE_OUTPUT_SCHEMA_INVALID` (the file with `none`, not JSON, not an object, not a schema, `$id` mismatch, an id clashing with a contract, an id with neither file nor contract) and `PROFILE_NOT_CHECKED` (bytes changed after the check). The system bytes and order are unchanged.
- (B) `golden_case.v1` requires `request.retrieval` and `expect.grounding`. A `disabled` case runs on a second `Pipeline` with `rag.enabled: false` and opens nothing; an `enabled` case under a rag-off configuration is refused before any case runs; a v0 file is refused by name.
- (C) `response_envelope.v1` has a required `retrieval.error`: null, or one closed code when the state is `failed`.
- (D) A grounding case never passes `degraded`: the schema forbids it and `golden_codes` returns `GOLDEN_GROUNDING_DEGRADED`.
- (E) `not_reached` covers retrieval on with no failure and no S3. Consumers: envelope, intake refusal envelope, golden judging, trace and eval columns (`tables.ENV`), `eval_run.ENVELOPE_SCHEMA`, docs.

**Commands and output.** Each file ran in its own process: `test_slot.py -- .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=... <file>`, with `PYTHONPATH=<worktree>/src`, `PYTHONNOUSERSITE=1` and `PYTHONDONTWRITEBYTECODE=1` (script `.tmp/T017-FIX3/run.sh`). Final results:

| File | Result |
|---|---|
| test_cli_enhance | 24 passed |
| test_config_links | 10 passed |
| test_enhance_serve | 21 passed |
| test_envelope | 10 passed |
| test_eval_cli | 9 passed |
| test_eval_concurrency | 13 passed (after its envelope double got `error`) |
| test_eval_config | 211 passed |
| test_eval_judge | 33 passed |
| test_eval_properties | 8 passed |
| test_eval_storage | 4 passed |
| test_field_contracts | 540 passed, 34 skipped |
| test_golden_diagnostics | 14 passed |
| test_golden_retrieval | 17 passed |
| test_intake | 76 passed |
| test_intake_config | 31 passed |
| test_intake_properties | 4 passed |
| test_model_client | 79 passed |
| test_model_client_image_side | 36 passed |
| test_output_schema | 16 passed |
| test_pipeline | 18 passed |
| test_pipeline_config | 62 passed |
| test_pipeline_s0_s3 | 1 passed |
| test_pipeline_s0_s6 | 14 passed |
| test_pipeline_s4_s5 | 1 passed |
| test_planner | 67 passed |
| test_profile_properties | 27 passed |
| test_profile_validators | 43 passed |
| test_profiles | 196 passed, 2 skipped |
| test_profiles_anchored | 22 passed |
| test_retained_images | 72 passed, 2 skipped |
| test_stage_flow | 24 passed |
| test_stage_revisions | 417 passed |
| test_trace_store | 7 passed |
| test_validation | 44 passed |
| test_validation_config | 169 passed |
| test_validation_layout | 2 passed |
| test_writer | 17 passed |
| test_writer_attachments | 18 passed |
| test_writer_config | 215 passed |

In the first full pass, `test_field_contracts` showed 572 setup errors, caused by a one-off run of mine that shared and deleted its basetemp. The rerun above was alone and is clean.

**Stage revision (needs PM attention).**
- `RoleClient._call` is pinned in the `model_call` inventory, and its writer branch now passes `profile_schema_id`. So `MODEL_CALL_REVISION` went from `model-call-5` to `model-call-6`, with a new pin row in `tests/test_stage_revisions.py` (digest `30e3a752…`).
- Consequence: the next `kb sync` recomputes the K2, K3 and K4 model-call artifacts once, although no K-role request changes (only the `role_name == 'writer'` branch differs).
- Alternative, if that recompute is unwanted: move the bundle-id acceptance out of `_call`. That needs a design choice, because `_call` is the only place that resolves the writer format per request.
- The four new binding helpers are in `stage_guard.LEFT_OUT`, each with its reason.

**Mutants** (scratch copy of `src` plus `contracts`, `prompts` and `configs` under `.tmp/T017-FIX3/mut`, script `.tmp/T017-FIX3/mut.py`):
Baselines (no mutation): `test_envelope` 10 passed, `test_golden_retrieval` 17 passed, `test_output_schema` 16 passed. All 10 mutants were killed:

| Mutant | Change | Killed by |
|---|---|---|
| m1 | `not_reached` fallback becomes `no_match` | `test_envelope` (2 failed: the property test, `test_failed_plan_after_ready_s1_is_not_reached_not_failed`) |
| m2 | drop the SNAPSHOT_MISSING branch of `_retrieval` | `test_envelope` (property test) |
| m3 | `golden_codes` ignores `degraded` | `test_golden_retrieval` (3 `test_golden_codes` cases) |
| m4 | no refusal of `enabled` under rag off | `test_enabled_case_under_retrieval_off_is_refused_before_any_run` |
| m5 | ragoff pipeline keeps `rag.enabled: true` | 3 tests incl. `test_disabled_case_opens_no_store...` |
| m6 | pipeline does not bind the bundle schema | `test_pipeline_binds_the_bundle_schema_to_each_request_client` |
| m7 | S5 registry without the bundle schema | `test_s5_accepts...`, `test_s5_repair_is_checked...` |
| m8 | `use_output_schema` leaves `client.schemas` stale | 4 tests in `test_output_schema` |
| m9 | resolve skips `output_schema_sha256` | `test_bundle_output_schema_is_bound_by_the_check[edit,none]` |
| m10 | `retrieval.error` takes the S1 `message` instead of `code` (R-026) | 3 tests in `test_envelope` |

(A first attempt ran without `contracts/` in the scratch tree and failed every test; it was discarded, and the baselines above prove the corrected tree.)

**not_run.**
- `ruff check` / `ruff format --check`: ruff is not installed in `.venv` (`No module named ruff`), and I did not install it (shared venv).
- Linux CI.
- Any live llama.cpp run: whether b11921 enforces every keyword of an admin schema is unmeasured; client-side validation is the guard.

**Changed files.**
- New schemas: `contracts/schemas/response_envelope.v1.schema.json`, `intake_result.v1.schema.json`, `golden_case.v1.schema.json`.
- `src/prompt_enhancer/`: `envelope.py`, `intake.py`, `eval_run.py`, `storage/tables.py`, `model_schemas.py`, `config.py`, `profiles.py`, `model_client.py` (outside 875-911), `pipeline.py`, `cli_commands.py`, `writer.py`, `validation.py`, `verifier_policy.py`.
- `tests/`:
  - new: `test_output_schema.py`, `test_golden_retrieval.py`;
  - changed: `profile_fixtures.py`, `test_profiles.py`, `test_eval_concurrency.py`, `test_pipeline_s0_s6.py`, `test_stage_revisions.py`, `stage_guard.py`, `test_intake.py`, `test_envelope.py`, `test_golden_diagnostics.py`, `test_pipeline.py`, `eval_fixtures.py`, `serve_fixtures.py`, `test_cli_enhance.py`, `test_enhance_serve.py`.
- Docs: `docs/stages/S0-intake.md`, `S4-writer.md`, `S6-envelope.md`, `P-profiles.md`, `docs/architecture.md`, `docs/run-guide.md`, `contracts/README.md`.

**Research evidence.** In Step 0 (src line references). The approach note `.tmp/T017-FIX3/rca3-report.md` is kept.

### Configuration audit

No `enhancer_config` key was added or changed. Every new literal is classified below (scan: `git diff -U0 -- src/ | grep -E '^\+'`).

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Bundle writer schema | admin bundle `<profile>/output_schema.json` + manifest `output_schema_id` | `load_output_schema` at every load; hash in activation | none shipped (the example keeps `none`) | override `test_writer_sends_bundle_schema_and_keeps_system_bytes`, `test_bundle_output_schema_is_bound_by_the_check`; refusal `test_bundle_schema_refusals` (7 cases), `test_profile_without_bundle_schema_binds_none` | artifact-bound file name `output_schema.json` |
| Golden retrieval switch | `golden_case.v1` `request.retrieval` | schema enum + check-time refusal under rag off | test fixtures only | override `test_disabled_case_opens_no_store_while_enabled_case_still_retrieves`; refusal `test_enabled_case_under_retrieval_off_is_refused_before_any_run`, `test_retired_golden_v0_case_is_refused_by_name` | protocol-bound enum |
| Grounding expectation | `golden_case.v1` `expect.grounding` | schema `if/then` | test fixtures only | `test_golden_codes`; refusal `test_case_schema_refuses_degraded_on_a_grounding_case` | protocol-bound enum |
| `retrieval.error` codes, `not_reached` | `response_envelope.v1` | schema enum + `if/then` | n/a (output contract) | the envelope property and closed-code tests | protocol-bound |
| Golden codes, `GOLDEN_RETIRED_VERSION = 1`, `s0-s6.v3`, schema ids v1 | n/a | n/a | n/a | `test_golden_codes`, the v0 refusal | protocol-bound / artifact-bound |
| Ragoff golden `Pipeline` (`rag.enabled: false` copy) | derived from the case, not configuration | — | — | the B test above | safety-bound (R-009) |

**Noticed, not done.**
- The retrieval-off golden or eval `Pipeline` has `release_hold=None`, so expiring a held trace there drops the hold without releasing it. This is the existing `eval_run` pattern; a hold needs retrieval on, so it is unlikely but not proven impossible.
- A bundle schema's external `$ref`s are not resolved at load. A `$ref` to another file fails at validation time, not at check.
- `configs/profiles/_example/` ships no `output_schema.json` and its README does not mention it. The PM migrates the pilot golden cases in `runs/` (untouched).
- `tests/test_retained_images.py:629` keeps an `intake_result.v0` comment that is still true of v1.
- Untouched, as out of scope: CLOUD-1 (`model_client.py` 875-911, cloud schema), the dropped-seed rule, multi-source, `prompts/`, and planner cross-field failures.
- `docs/implementation-notes.md` (PM-owned move): decision text for 194 and 197-200 as built above.

**Open issues.** None blocking. A PM decision is pending user review: a `golden_case.v0` file is refused rather than read with a guessed meaning (approved in phase 1, answer 2).
