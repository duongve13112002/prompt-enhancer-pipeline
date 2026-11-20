# MS-3: request and intake for several source images (request.v0 and request.v1, S0)

Kind: code
R-IDs: R-001, R-002, R-004, R-009, R-015, R-026
Task file: tasks/active/MS-3-request-intake.md
Approach note: `.tmp/MS-3/MULTI-SOURCE-step0.md` (decisions 176, 177, 179, 182); base 1100f90
Trivial: no. Small: no. Status: phase 2 done (built, not committed), waiting for review.

## Step 0 (approved by the PM as written; condensed in phase 2)

**Evidence (source-verified at 1100f90).** S0 (`intake.py`) decoded one `source_image` and stored `payload['source_image']`; `cli_input.parse_request` (shared by the request file and the HTTP body, `cli_serve.py:291`) hard-coded `request.v0` and repeated the `edit`/`generate`/base64 rules that `intake.validate_request` also held (two copies). `max_decode_pixels` applies only in intake, `max_request_bytes` only at the read. Single-source readers after intake: `planner.py:384`, `writer.py:160`, `envelope.py:93` (also reached for refused results) and `pipeline.py:284` (nothing stopped n of 2 or more); S1, S3, S5, the verifier read no image; GoldenAdapter and eval build a v0 request with one image. `request.v1` and `intake_result.v2` (MS-1) leave base64 validity, the caps, duplicates and index continuity to code. Stale: the "for every request" lines about `intake_result.v2` (design Q3 keeps n of 1 on today's shape), the design's version names (`intake_result.v1`/`response_envelope.v1` for multi-source; real: v2 and MS-5's `response_envelope.v2`), `GoldenAdapter.identity` (already `s0-s6.v3`).

**Approved answers.** (1) Intake builds n images; the pipeline refuses n of 2 or more before S1 with `MULTI_SOURCE_UNAVAILABLE` (`intake.refuse_multi_source`), a failed `response_envelope.v1` with `source_binding: null`. (2) One image, from `request.v0` or a one-item `request.v1`, keeps today's payload; `intake_result.v2` only for n of 2 or more. (3) Codes: `SOURCE_IMAGE_REQUIRED`, `SOURCE_IMAGE_UNEXPECTED`, `SOURCE_IMAGES_CONFLICT` (new), `SOURCE_IMAGES_TOO_MANY`, `SOURCE_IMAGE_DUPLICATE`, `IMAGE_UNDECODABLE`/`IMAGE_REJECTED` with the index; a refused `request.v1` gets the index-naming text, `request.v0` text is unchanged. (4) Cap `min(edit.max_source_images, renderer.max_source_images)`, 1 for a version-1 manifest. (5) Shared rules in the new `request_shape.py`.

## Steps

1. Captured baselines on unchanged code (`tests/fixtures/ms3_baseline/`, 32 KB and 6 KB): production S0 to S6 for a one-image edit (model-call bodies, envelope, stage bodies), S0 payloads and parser outputs for eight v0 cases. Only the random trace ids and per-run call timings are masked.
2. `request_shape.py` (new), `cli_input.parse_request`, `intake.py` (cap, count-first refusal, duplicates, per-image decode and deadline, v1/v2 payload, `refuse_multi_source`, safe-text flag), `envelope.py` (v2-aware binding), `pipeline.py` (one call), `__main__.py` help.
3. Tests: `test_ms3_one_image_identity.py`, `test_ms3_request_intake.py`, harness `ms3_fixtures.py`; docs in `docs/stages/S0-intake.md` and `contracts/README.md` (no schema file touched).
4. Ran the pipeline list and the guards, then 12 mutants.

## Completion report

**Behaviour.** `request.v0` and `request.v1` are told apart by the field present (`request_shape.request_version`); both fields is `SOURCE_IMAGES_CONFLICT`. One image, in either shape, gives today's `intake_result.v1` payload and today's bytes through S1 to S6 (baseline tests for a v0 request and a one-item v1 request). Two or more build `intake_result.v2` (`request_version`, `source_images[{index (1-based), image_id, role, data}]` in request order, no `source_image` key); `Pipeline._run_request` then calls `refuse_multi_source` before S1, giving `MULTI_SOURCE_UNAVAILABLE` and a failed `response_envelope.v1` (null output, `source_binding: null`), with no model request at all. The cap is read after the profile resolves and a list over it is refused on its length before any base64 or Pillow work; duplicates (same bytes after base64 decoding) are refused before any Pillow work; the deadline is checked between images with the existing stage labels. A refusal text is built by code from field names, indexes and integers only (`_Refusal.safe`); everything else still shows only its code or class name. `envelope.build_envelope` raises `NotImplementedError` for a non-refused v2 payload rather than bind image 1. No config key, schema, prompt or manifest changed.

**Commands (each through `tools/agent-workflow/test_slot.py`, `.venv` python, `PYTHONPATH` = `<worktree>/src`, one file per process).** Result lines:
- `test_ms3_request_intake.py`: 47 passed. `test_ms3_one_image_identity.py`: 3 passed. `test_multi_source_contracts.py`: 97 passed (the 15 `intake_result.v2` cases now run). `test_ms2_config_v2.py` (GUARDED hashes): 115 passed. `test_ms2_manifest_v1.py`: 97 passed.
- `test_intake.py`: 76 passed. `test_intake_properties.py`: 4 passed. `test_cli_inputs.py`: 70 passed, 1 skipped. `test_cli_properties.py`: 10 passed. `test_cli_enhance.py`: 24 passed. `test_enhance_serve.py`: 21 passed; `_properties` 10, `_delivery` 16, `_transport` 2 passed.
- `test_pipeline.py`: 18 passed. `test_pipeline_s0_s6.py`: 14 passed. `test_stage_flow.py`: 24 passed. `test_envelope.py`: 10 passed. `test_writer.py`: 17 passed. `test_writer_attachments.py`: 18 passed. `test_planner.py`: 67 passed. `test_stage_revisions.py` (`stage_guard`): 441 passed. `test_field_contracts.py`: 529 passed, 55 skipped (data-dependent skips at `test_field_contracts.py:177,224,247,259`, none from this change). `test_retained_images.py`: 72 passed, 2 skipped. `test_golden_retrieval.py` 17, `test_golden_diagnostics.py` 14, `test_profiles.py` 196 passed 2 skipped, `test_eval_cli.py` 9, `test_eval_properties.py` 8 passed.
- Property test: `test_parser_accepts_exactly_what_the_schema_and_rules_allow` (hypothesis 6.170.15 present in `.venv`, 250 examples): an oracle from the contract (not from the code) over task, shape (none, v0, v0 null, v1, both), n of 0 to 4 and base64 validity; accepted output equals the input and keeps image order.

**Mutants (scratch copy of `src`, run by `.tmp/MS-3/mutate.py`, removed afterwards).** 12 run, 12 killed:

| Mutant | Killed by |
| --- | --- |
| M1 drop the count check | `test_the_cap_is_the_smaller_of_config_and_manifest` |
| M2 count after decoding | same (the `_image` and Pillow spies see calls) |
| M3 reverse the image order | `test_several_images_build_intake_result_v2_in_request_order` |
| M4 drop the duplicate check | `test_duplicate_bytes_are_refused_before_any_pillow_call` |
| M5 `intake_result.v2` at one image | `test_a_one_item_list_keeps_the_one_image_payload` |
| M6 no deadline check between images | `test_the_deadline_is_checked_between_images` |
| M7 index starts at 0 | `test_several_images_build_intake_result_v2_in_request_order` |
| M8 binding of image 1 at several | `test_the_pipeline_refuses_several_images_before_any_model_call` |
| M9 no pipeline refusal | same |
| M10 every refusal text shown (safe flag off) | `test_the_deadline_is_checked_between_images` first (`-x`); the baseline and R-026 tests also pin it |
| M11 cap is the larger limit | `test_the_cap_is_the_smaller_of_config_and_manifest` |
| M12 parser accepts both fields | `test_v1_refusals_name_the_field_and_the_index_and_hold_no_value` |

**Configuration audit (row by row).**

| Operational value or behaviour | Validated path | Validation | Example or profile | Override and refusal test | Class |
| --- | --- | --- | --- | --- | --- |
| Source cap (config side) | `edit.max_source_images` | MS-2 at load; S0 re-checks with `check_limit` when n is 2 or more (missing, sentinel, 0, bool, float refused) | `enhancer.example.yaml:144` (`deploy_required`), dev examples (MS-2) | accepted at the cap, refused at cap + 1 with nothing decoded (`test_the_cap_is_the_smaller_of_config_and_manifest`); refusals in `test_source_cap_of_a_version_1_manifest_and_a_missing_limit` | configurable |
| Source cap (manifest side) | `renderer.max_source_images` | MS-2 at profile load; S0 re-checks | `_example/manifest.yaml` (MS-2) | cases with manifest 2 and config 5 (and the reverse) in the same test | configurable |
| Version-1 manifest means one image | `MANIFEST_V0_LIMIT = 1` in `intake.py` | none to validate: the meaning of a manifest without a limit (MS-2, `profiles.py:535-547` refuses a cap of 2 or more with it at load) | n/a | `test_a_version_1_manifest_means_one_image` (cap 1, n of 2 refused; n of 1 accepted) and the direct `source_cap` test | protocol-bound |
| Decode bound, resize, quality | `vision.max_decode_pixels`, `vision.*` | unchanged (existing) | unchanged | `test_an_image_over_the_decode_bound_is_rejected_with_its_index`; `test_intake.py` existing override tests pass | configurable (unchanged) |
| Intake deadline | `runtime.intake_deadline_s` | unchanged | unchanged | `test_the_deadline_is_checked_between_images` | configurable (unchanged) |
| Request size bound | `serving.http.max_request_bytes` and the `--request` limit | unchanged (read at the file and the HTTP body, `cli_serve.py:268`, `cli_input.py:132`); it now bounds all images together | unchanged | existing `test_cli_inputs.py`, `test_enhance_serve*.py` pass | configurable (unchanged); run guide wording is a docs follow-up |
| Request cannot raise a limit | `request_overrides.allowed_keys: []` | unchanged (MS-2 refuses limit keys in the allowlist) | `enhancer.example.yaml:187` | not touched | safety-bound |
| Refusal codes and fixed texts | `intake.py` constants (`TOO_MANY_CODE`, `DUPLICATE_CODE`, `MULTI_SOURCE_CODE`, the message), `request_shape.py` (`CONFLICT`, `REQUIRED`, `UNEXPECTED`) | n/a | n/a | exact-text tests of every refusal | protocol-bound |
| Schema ids and field names (`request.v0`, `request.v1`, `source_image`, `source_images`) | `request_shape.py` | n/a | n/a | parser and intake tests | artifact-bound |
| Test constants (caps 1 to 5, `max_decode_pixels` 10000, colours, markers) | tests only | n/a | n/a | n/a | test-only |

No hidden default: a missing or sentinel limit raises `ConfigurationError` (precise key named); no limit is invented. No new configuration key, so no example-profile change.

**`not_run`.** `ruff check` and `ruff format --check`: ruff is not installed in `.venv` (`python -m ruff` gives "No module named ruff"); I did not install it (rule 1). The tool suite `tools/agent-workflow/tests/`: not applicable (no tooling change). No live model call, no GPU, no CI (Linux) run: all tests are CPU, Windows only here.

**Changed files** (`git status`): `src/prompt_enhancer/{request_shape (new),cli_input,intake,envelope,pipeline,__main__}.py`; `tests/{ms3_fixtures,test_ms3_one_image_identity,test_ms3_request_intake}.py` and `tests/fixtures/ms3_baseline/{edit_one_image,intake_one_image}.json` (new); `docs/stages/S0-intake.md`, `contracts/README.md`; this file. No file under `contracts/schemas/`, `prompts/`, `configs/`; no GUARDED file; none of `config.py`, `profiles.py`, `writer.py`, `planner.py`.

**Open issues and decisions to record.** (1) `SOURCE_IMAGES_CONFLICT` and its text, and the "; send one of them" remedy, are new wording: for the PM's review. (2) `NotImplementedError` in `build_envelope` for a non-refused v2 payload is a deliberate loud stop that MS-5 replaces. (3) Index in `IMAGE_REJECTED`/`IMAGE_UNDECODABLE` messages only for `request.v1`; `request.v0` text is byte for byte as before (baseline test). (4) The HTTP route reaches the new parser through the unchanged `parse_request` call; no HTTP-level v1 test was added beyond the existing serve suites (parser-level tests cover the wording and the 400 mapping is unchanged). (5) The run guide should say that `max_request_bytes` bounds all images together (not edited: outside this scope).

## Noticed, not done

- `intake._refuse` redacts every non-listed exception to its class name while `parse_request` gives exact-field text: two message styles; kept.
- `contracts/README.md` still has several "designed, not yet implemented" lines for S2 to S6; they stay true until MS-4 and MS-5.
- `docs/stages/S0-intake.md:35` still carries "PM decision pending user review" for `SOURCE_IMAGE_UNEXPECTED`; untouched.
