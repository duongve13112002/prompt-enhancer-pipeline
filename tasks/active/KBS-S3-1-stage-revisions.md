# KBS-S3-1: declared stage revisions instead of module-source hashing

Status: **QA fix round 1 (Claude Sonnet) complete 2026-11-19, awaiting re-review; guard, regression sets green, every mutant killed.** Design accepted by the user (decision 167).

## Task brief

- **Goal:** artifact keys of the offline knowledge-base stages change when the code that shapes an artifact changes, and only then (R-017). Before: `tool_revision` hashed whole module source files (including `config.py`), so any edit re-embedded every image and redid every model-produced card and alias (finding KBS-S3-1, smoke 3).
- **Design:** a declared artifact-bound revision per stage in the key, plus a guard pinning an `ast.dump` digest of the stage's output-shaping code. **Requirements:** R-015, R-017. **Out of scope:** configuration leaves, model choice, prompts, what a stage produces.
- **Done check:** no key or fingerprint in `src/` depends on module source text; each stage key carries its declared revision; each stage has a guard pinning its inventory.
- **Constraints:** no Git writes, installs, network, GPU or live model call; `.secrets/` untouched; ruff not installed (lint `not_run`).

## Step 0 (implementer): inventory and plan

**Unknowns resolved from the code.** (1) Besides the seven `tool_revision` callers, three more source-text fingerprints existed: `k0_checkpoint._REVISION`, `k5_index._tools` (`code`, `normalization`) and `kb_sync.sync_identity`. `profiles._code_fingerprint` fingerprints a profile verdict, not an artifact: left (Noticed). (2) Stored rows and model payloads built inline in 200-line drivers cannot be pinned without pinning concurrency and reporting, so each layout lives in a small named function (behaviour unchanged). (3) The sync identity now carries the declared revisions.

**Design.** One constant per stage in its own module, entering the key as `'stage_revision'` (K2, K3, K4 also carry `'model_call': MODEL_CALL_REVISION`). Guard: `tests/stage_guard.py` (inventory, `ast.dump` of listed names without docstrings or positions, Python 3.12 format as decision 124) and `tests/test_stage_revisions.py` (append-only `PINS` history of `(revision, digest)` per stage; a repeated revision or digest is refused, so an in-place edit shows as a rewritten row). Unchanged in keys: `PIL.__version__`, layout versions, configured values.

**Inventory** (full list in `tests/stage_guard.py`; one line per stage). K0 `K0_SCAN_REVISION`: info parsing, scan rows, checkpoint layouts, `read_source_file`, `concept_id`, `image_id`. K1 image `K1_IMAGE_REVISION` and K1 text `K1_TEXT_REVISION`: image preparation, request bodies, reply parsing, stored rows, **`_image_order`, `_text_order`, `_chunks`, `Embedder._embed/_post/embed_*`, `_commit_record`, `canonical_json`**. `MODEL_CALL_REVISION`: `RoleClient._call`, request mapping, reply parsing, schema validation, prompt bytes, `_usage`, `NO_LIMIT`, `SAMPLING`, `LOCAL_REASONING_EFFORT`, `MAX_JSON_DEPTH`. K2 naming `K2_NAMING_REVISION`, **K2 draw `K2_DRAW_REVISION`** (new, value `k2-draw-v1` unchanged so persisted draws stay valid: draw, redraw, override and applied-name artifacts were keyed by `DRAW_REVISION` that no guard covered), K2 observe `K2_OBSERVE_REVISION` (+ `_member`, `_declared`), K3 `K3_CARD_REVISION`, K4 `K4_ALIAS_REVISION`, K5 `K5_INDEX_REVISION` (+ `_tools`, `_metadata`, `_part_entry`, `SCHEMA`, `MANIFEST`, `INPUTS`, `VECTOR_TABLES`, `SCORE_DTYPE`, `NORMALISATION_RULE`), `kb sync` `KB_SYNC_REVISION` (sync identity only). `storage.identity.canonical_json` is pinned in every stage that stores its output (K1 to K5, draw).

**Left out on purpose:** `LEFT_OUT` in `tests/stage_guard.py` names every other top-level function, method and constant of the covered modules with a one-line reason (key composition, drivers, lifecycle and credentials, error codes, probes, validators, file access, reuse choice, serving search, writer-side profile parsing; `config.py` except `image_media_type` and `TEXT_PLACEHOLDER`).

**Questions (proposed answers, none blocking).** (1) K0 and K5 are cheap and included because no key may depend on module source. (2) `serving_index.v0` requires `tools.code` and `tools.normalization`: filled with `K5_INDEX_REVISION` and `nfkc-casefold`. (3) Pins are Python 3.12 digests; the guard is skipped on another minor version (CI and dev venv run 3.12).

## QA fix round 1 (review `.tmp/KBS-S3-1-rev/review-claude.md`, verdict FAIL on P1-F1)

| Finding | Fix |
|---|---|
| P1-F1 K1 batch composition and input order unpinned | `_chunks` pinned in both K1 inventories; the two sorts extracted to `_image_order` and `_text_order`, pinned in their part; `Embedder._embed/_post/embed_images/embed_texts` pinned. `embedding.batch_size` stays out of the K1 key **by design** (decision: the pinned batching code stands for it; changing batching code needs a K1 revision bump); documented in `docs/stages/K1-embed.md`. |
| P2-F2 no completeness check | `test_every_function_and_constant_is_pinned_or_left_out_with_a_reason` (fails on an unclassified, stale or doubly classified name) plus `test_a_new_unclassified_function_in_a_stage_module_is_reported`. |
| P3-F3 `canonical_json` only in K5 | pinned in K1 to K5 and draw. |
| P3-F4 K2 observe `declared` layout and sort inline | extracted `_member`, `_declared`; pinned. |
| P3-F5 `_usage` unpinned | pinned in model call. |
| P3-F6 K5 `_metadata` and inline `parts` entry | `_part_entry` extracted; `_metadata`, `_tools`, `_part_entry` pinned. |

All revisions moved with their pin rows (new rows appended, none edited): `k0-scan-v5`, `k1-image-2`, `k1-text-2`, `model-call-2`, `k2-naming-2`, `k2-observe-2`, `k3-card-2`, `k4-alias-2`, `k5-index-2`; new stage `k2_draw` starts at `k2-draw-v1`; `kb-sync-1` unchanged (its inventory did not change).

## PM integration fixes (retest `.tmp/KBS-S3-1-rt/retest-claude.md`, FAIL on NF1, NF2; decision 164: no second fix round)

| Finding | Fix | Evidence |
|---|---|---|
| P2-NF1 K1 batch membership inline in `embed_images` | `_batch_members(chunk, records)` and `_companion_lost(image_id, outcome, records)` extracted (byte-equivalent conditions) and pinned in `k1_image`; `K1_IMAGE_REVISION` `k1-image-3` with an appended pin row; `k1_text` digest unchanged. New test `test_a_partially_committed_image_batch_keeps_its_cached_successful_members`. | mutant MP (`_batch_members` drops cached successful members): killed by the new test and by `tests/test_res1_resume.py` (4 failed: `test_k1_every_checkpoint_boundary[images]`, three `test_k1_interrupted_done_companion_is_immutable` cases); the retest had not run that file. |
| P2-NF2 `K2_DRAW_REVISION` missing from the sync identity | `'k2_draw': K2_DRAW_REVISION` added to the `kb_sync` `code` identity. The draw stage makes no model call, so a separate test replaces the call-count parametrization: `test_a_bumped_k2_draw_revision_changes_the_sync_identity` (unchanged before the bump, published after, no new embedding). | mutant (entry removed): the new test fails (`unchanged` != `published`). |

Notes kept from the retest (not defects): the completeness test does not cover `model_transport`, `model_resources`, `model_probe`, `model_secrets`, `_os_support` or `storage.*`; they act by timing, transport or file access only, so nothing forces a new content-shaping function there to be classified. `embedding.batch_size` stays out of the K1 key, as before this task; `docs/stages/K1-embed.md` says a changed value alone keeps every stored vector.

Commands (PM, `test_slot.py`, one file per process, `PYTHONPATH` = worktree `src`): `test_stage_revisions.py` 381 passed; `test_k1_embed.py` 68 passed, 1 skipped; `test_res1_resume.py` 9 passed; `test_kb_sync_invalidation.py` 26 passed; `test_res1_decode_stop.py` 7 passed.

## Completion report

**Work completed.** Every K0 to K5, `kb sync` and model-call key carries a declared revision; no artifact key or fingerprint in `src/` reads module source (`test_sources_hold_no_module_source_fingerprint`). Eleven guards pin the inventories; behaviour is unchanged (layouts moved into named helpers: `_image_record`, `_text_record`, `_image_order`, `_text_order`, `_naming_result`, `_observation_payload/_bundle`, `_member`, `_declared`, `_grouping_bundle`, `_card_revisions`, `_alias_payload`, `_generation_bundle`, `_alias_rows`, `_part_entry`).

**One-time key change (expected).** On the first run after this change every stage key differs (old `tool` digest replaced by `stage_revision`; K5 `tools.code` is `k5-index-2`; the sync identity changes). The next `kb sync` recomputes K0 to K5 once, and published snapshots no longer match the K5 tools fingerprint until it republishes. Draw artifacts keep their keys (`k2-draw-v1`). From then on an edit outside the pinned code changes nothing.

**How a developer uses the guard.** Edit a pinned function: the stage fails with its new digest in the message; bump the constant and append `(new revision, new digest)` to `PINS`. Add a function to a covered module: pin it in `tests/stage_guard.py` or list it in `LEFT_OUT` with a reason, or the completeness test fails.

**Result and evidence** (interpreter `.venv/Scripts/python.exe`, `PYTHONPATH` = worktree `src;tests`, every command through `tools/agent-workflow/test_slot.py`, `--basetemp .tmp/KBS-S3-1/pytest`, removed afterwards). Phase 1 per-file results: `.tmp/KBS-S3-1/results.txt`; round 2: `.tmp/KBS-S3-1/round2-results.txt`.

| Set | Result |
|---|---|
| `tests/test_stage_revisions.py` (guard, properties, completeness, helper mutants, key and sync tests) | 375 passed in 56 s |
| Round 2 regression, one process: `test_k1_embed/_text`, `test_k2_naming/_observe/_fix_round1-3/_name_override/_semantic_keys/_observe_shared_keys`, `test_k3_cards/_fix_round1-2`, `test_k4_aliases`, `test_k5_index/_properties`, `test_k0_checkpoint/_scan`, `test_model_client_image_side`, `test_embed_server_identity`, `test_kb_sync_invalidation/_flow/_gates/_diagnostics` | 895 passed, 1 skipped (existing skip) in 552 s |
| Phase 1 files not rerun in round 2 (config and edge suites; no source they exercise changed beyond refactors the rerun files cover) | all passed in phase 1 |

**Mutants** (scratch copies under `.tmp/KBS-S3-1/mut`, removed; `.tmp/KBS-S3-1/mutants2.json`; no revision bump, no pin row; killed = failure of `test_pinned_inventory_matches_declared_revision` or the completeness test). Phase 1 M1 to M7 stay killed (`.tmp/KBS-S3-1/mutants.json`).

| Mutant (no bump, no pin row) | Result |
|---|---|
| MF `_chunks` reversed per batch | killed: `[k1_image]`, `[k1_text]` (2 failed) |
| MG `canonical_json` `ensure_ascii=True` | killed: 8 stage pins failed (K1 image and text, naming, draw, observe, K3, K4, K5) |
| MH `_usage` drops zero counts | killed: `[model_call]` |
| F1 `_image_order` descending | killed: `[k1_image]` |
| F1 `_text_order` descending | killed: `[k1_text]` |
| F4 `_declared` descending | killed: `[k2_observe]` |
| F4 `_member` layout | killed: `[k2_observe]` |
| F6 `_part_entry` size + 1 | killed: `[k5]` |
| F2 new unclassified function `_fresh_helper` in `k1_embed` | killed: completeness test |

**Problems, risks and remaining work.** (1) `RoleClient._call` and `kb_sync.sync_knowledge` are pinned whole (coarse on purpose: a probe or fallback edit also needs a `MODEL_CALL_REVISION` bump); splitting `_call` is out of scope. (2) Pins are Python 3.12 digests. (3) Left-out reasons are one line each and a reviewer may disagree with a classification; the list is in one place.

**Noticed, not done.** `profiles._code_fingerprint` (profile verdict fingerprint) has the same over-approximation; not a knowledge-base key. Archived notes (`T-011-k4-k5.md`, `RES-1-resource-use.md`) still describe `tool_revision` (history).

**not_run.** Lint (`ruff` not installed). Tool suite `tools/agent-workflow/tests/`. The full suite. GPU, live model or network. A second Python minor version for the pins. A `kb sync` on a real knowledge base.

### Research evidence

| Question | Source | Finding | Class |
|---|---|---|---|
| What does R-017 require of the revision in a key? | `docs/execution.md` lines 74-78 | key = input hash plus model, prompt and schema revisions; code version is declared | documented |
| Is an `ast.dump` digest an accepted code identity? | `src/prompt_enhancer/profiles.py:139-150` (decision 124) | docstrings dropped, no attributes, format tied to the Python version | source-verified |
| Does the K5 contract allow other `tools` keys? | `contracts/schemas/serving_index.v0.schema.json` | `tools` requires exactly `numpy, arrow, unicode, code, normalization, schema`; a run without `code` failed `K5: invalid metadata contract` | experiment |
| Does batch composition change vectors? | `k1_embed._chunks` docstring and comments (T-018) | a batch changes bf16 vectors, hence order and chunking are pinned | documented |

### Configuration impact

None: no configuration leaf is added or changed; no profile, schema or example changes. Revision constants are artifact-bound code identities. `embedding.batch_size` keeps its existing leaf and stays out of the K1 key by design (above).

### Configuration audit

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| `K0_SCAN_REVISION`, `K1_IMAGE_REVISION`, `K1_TEXT_REVISION`, `K2_NAMING_REVISION`, `K2_DRAW_REVISION`, `K2_OBSERVE_REVISION`, `K3_CARD_REVISION`, `K4_ALIAS_REVISION`, `K5_INDEX_REVISION`, `MODEL_CALL_REVISION`, `KB_SYNC_REVISION` | none (code identity) | guard pins each against its code | none needed | not a setting; effect on keys tested (`test_*_carry_*revision*`, sync-level tests) | artifact-bound |
| `NORMALISATION_RULE = 'nfkc-casefold'` | none | `serving_index.v0` requires the key; pinned by the K5 guard | none | not a setting | protocol-bound |
| Configured values in keys (model, prompt, schema, vision, `max_decode_pixels`, `PIL.__version__`, layout versions, `facet_schema_version`, backend extras) | unchanged | unchanged | unchanged | existing per-stage override tests pass (`test_k1_config`, `test_k2_config`, `test_k2_observe_shared_config`, `test_k2_semantic_keys`, `test_k3_config`, `test_k4_config`, `test_embed_server_identity`) | unchanged |
| `embedding.batch_size` | `embedding.batch_size` (existing) | existing | existing | existing; out of the key by design, covered by the pinned `_chunks` | configurable |

No new leaf, hidden default or unclassified literal.

**Changed files.** `src/prompt_enhancer/{k0_checkpoint,k1_embed,k2_naming,k2_observe,k3_cards,k4_aliases,k5_index,kb_sync,model_client}.py`; `tests/stage_guard.py`, `tests/test_stage_revisions.py` (new), `tests/test_kb_sync_invalidation.py`; `docs/execution.md`, `docs/stages/K1-embed.md`, `docs/stages/K5-index.md`; this file.
