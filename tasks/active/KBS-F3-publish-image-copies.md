# KBS-F3: knowledge-base publish keeps hash-checked image copies

Kind: code. R-IDs: R-009, R-011, R-014, R-017, R-024, R-026. Decisions 160, 161 (copy every distinct scanned image, original bytes, hash- and size-checked; per-image and total limits `deploy_required`), 163 (a snapshot without copies refuses attachment requests before S4 naming `kb sync`; a failed copy fails the whole publish with CURRENT unchanged; no format setting), 164 (one build, one review round, this file under about 20 KB). Base: main `723bffe` plus the uncommitted change in this worktree. Scout note: `.tmp/KBS-F3/scout-note.md` in the main tree.

## Why

T-015 finding F2: a published snapshot held `images` rows but no image bytes, so `edit.attach_kb_reference_images: true` could not attach a knowledge-base image. Goal: `kb sync` writes a committed, hash-checked copy of every distinct scanned image; S3 selects, S6 attaches from the pinned snapshot, never the source tree (R-014); the preflight refusal is lifted for snapshots that carry copies.

## Step 0 (settled)

Unknowns: source replacement or link escape during copy; incomplete or corrupt coverage; legacy snapshots and rollback; publish atomicity; bounded resource use. Decisions 161 and 163 answered the scout's questions. The fix round (decision 164) settled four points as PM decisions pending user review: D1 intake reads no image bytes (manifest check only), bytes of the pack's `attach` images are read and hash-checked right after S3 and before S4; D2 no snapshot at all keeps the degraded `SNAPSHOT_MISSING` path, only a snapshot without copies refuses; D3 every retention-unavailable refusal is `KNOWLEDGE_FAILED` with the `kb sync` message; D4 pin cost unchanged (open issue below).

## Configuration impact

- `sync.retained_images.max_image_bytes`, `sync.retained_images.max_total_bytes`: required positive native integers (schema, `validate_sync_config`); `deploy_required` in `configs/enhancer.example.yaml`; explicit dev-only smoke values (16 MiB, 1 GiB, not measured) in `configs/enhancer.dev.example.yaml`. The total counts distinct images once. Both enter sync identity.
- `knowledge.image_extensions`, `storage.kb_store`, `storage.data_root`, `knowledge.root`: existing validated settings, unchanged; copies live under the fixed artifact-bound `images/<sha256>` path plus the marker `images/retention-v1` (one shared constant, `storage/store.py` `RETAINED_IMAGE_MARKER`).
- `edit.attach_kb_reference_images`, `edit.max_kb_reference_images`, `assembly.max_images_per_concept`: existing; the preflight refusal of `true` is gone, availability is checked against the pinned snapshot (intake, then after S3).
- `vision.format`, `vision.max_image_side`: model transport only; retained bytes are the originals. No model, provider, device, concurrency, timeout or lifecycle setting added.

## Completion report

**Status: ready for review.** Every finding of review 1 (`.tmp/KBS-F3-rev/review-claude.md`) and review 2 (`.tmp/KBS-F3-rev2/review-opus.md`) is fixed with a named test, except review-2 P3-C (D4: open issue). Production behaviour changed in four places: intake (manifest-only check, no-snapshot stays degraded, `KNOWLEDGE_FAILED`), `pipeline.require_attachment_copies` (pre-S4 byte check, refusal through `intake.refuse_attachments`), `storage/store.py` (`retained_image_entry`, `check_retained_images`, `RetainedFileError`, shared marker constant), `kb_sync.retain_images` (returns the retained ids; the published `images` table must equal them).

### Findings to fixes

| Finding | Fix | Test (all in `tests/test_retained_images.py` unless named) |
|---|---|---|
| R1 P2-1, P3-1 no-follow, link matrix | junction (`mklink /J`, `CREATE_NO_WINDOW`) on Windows, symlink on POSIX; skip only if not creatable | `test_directory_link_refused_on_every_source_component[folder, nested_ancestor, source_root_ancestor]`, `test_file_symlink_refused` (skipped here: no symlink privilege), `test_publish_refuses_link_and_keeps_current`, `test_link_inside_published_snapshot_refused_on_read[images_directory]` (`[copy_file]` skipped here) |
| R1 P2-2, R2 P2-B intake coverage | D1 manifest check per row, three images, damage on the last id | `test_intake_refuses_damaged_retention_before_s4[last_copy_removed, marker_removed, other_layout, unlisted_row, row_size_differs]`, `test_intake_accepts_three_published_images_and_never_reads_image_bytes` |
| R1 P2-3 per-request cost | D1: marker read only; selected `attach` images read after S3 | same, plus `test_same_size_corruption_is_caught_after_s3_for_selected_images_only` |
| R1 P2-4 delivery flow | one CPU `Pipeline` test, labelled doubles: model server, S2 plan, K3 stability | `tests/test_pipeline_s0_s6.py::test_published_image_attaches_after_source_removed_and_corrupt_copy_refuses_before_s4[False, True]` |
| R1 P2-5 validators | Hypothesis: malformed ids, uppercase hex, duplicates, negative, bool and non-int sizes, empty membership | `test_malformed_ids_refused`, `test_uppercase_hex_id_refused`, `test_negative_bool_and_non_integer_sizes_refused`, `test_duplicate_ids_and_empty_membership_refused` |
| R2 P2-A unique total | Hypothesis over 1 to 4 distinct payloads with multiplicities; publish boundary sum vs sum-1, CURRENT unchanged, `.building` removed | `test_unique_total_is_sum_of_distinct_sizes`, `test_publish_unique_total_boundary_keeps_current_and_cleans_up` |
| R1 P3-3 legacy | legacy CURRENT (labelled double of the old publisher) republished once, then `unchanged` | `test_legacy_current_is_republished_once_then_unchanged` |
| R1 P3-4 table vs copies | `sync_knowledge` refuses when the K1 `images` ids differ from the retained ids | `test_publish_refuses_when_images_table_differs_from_retained_copies` |
| R1 P3-5, docs | dev example values; `docs/stages/K0-sync.md`, `S6-envelope.md:18`, `S4-writer.md`, `contracts/README.md` state retention as built and D2 | `test_dev_example_declares_explicit_retention_limits_and_refuses_bad_ones` |
| R1 P3-6 R-009 | retrieval off with attach on opens no store | `test_retrieval_off_with_attachments_on_opens_no_store` |
| R2 P3-A (D2) | no snapshot: degraded `SNAPSHOT_MISSING`; copies missing: refuse | `test_no_snapshot_with_attachments_on_stays_degraded`; refusals above; `tests/test_intake.py::test_legacy_snapshot_refuses_edit_attachments_before_model_work` |
| R2 P3-B (D3) | `AttachmentRetentionUnavailable` is a `StorageError` (`KNOWLEDGE_FAILED`, fixed message); a missing or wrong-size manifest file raises `RetainedFileError`, mapped at pin for attach requests | `test_intake_refuses_damaged_retention_before_s4[last_copy_removed]` (code and message asserted by `assert_kb_sync_refusal`) |
| R2 P3-D, P3-E | docs; one marker constant; dead `except AttachmentRetentionUnavailable: raise` and the class removed from `config.py` | docs diff; `test_intake_refuses_damaged_retention_before_s4[other_layout]` (publisher marker differs from intake's constant) |
| R2 P3-C pin cost | not changed (D4) | open issue below |
| R2 identity | direct assertion for each limit | `test_each_limit_enters_sync_identity` |

### Commands

Each: `<main .venv>/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <same python> -X faulthandler -m pytest -q -p no:cacheprovider tests/<file> --basetemp=.tmp/KBS-F3-fix/bt`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`, one file per process, one at a time. Results on the final code:

```text
test_retained_images 63 passed, 2 skipped in 52.83s     test_kb_sync_flow 28 passed in 80.56s
test_intake 71 passed in 26.50s                         test_kb_sync_gates 19 passed in 71.17s
test_pipeline_s0_s6 14 passed in 34.51s                 test_kb_sync_invalidation 26 passed in 43.78s
test_storage_snapshots 17 passed in 8.61s               test_kb_sync_diagnostics 10 passed in 13.02s
test_k5_index 57 passed in 33.37s                       test_kb_sync_production_probes 1 passed in 4.41s
test_assembly 45 passed in 5.31s                        test_k0_sync 24 passed in 3.00s
test_assembly_config 18 passed in 1.45s                 test_loader 134 passed in 12.80s
test_pipeline_config 62 passed in 2.95s                 test_field_contracts 514 passed, 34 skipped in 172.47s
test_trace_store 7 passed in 1.52s                      test_storage_config 10 passed in 6.92s
test_pipeline 18 passed in 8.82s
```

test_storage_snapshots, test_k5_index, test_assembly, test_pipeline_config, test_trace_store, test_pipeline ran before the last `kb_sync.py` edit (table-vs-copies check); that edit touches none of them. The two skips in `test_retained_images` are the file-symlink variants (`file symbolic link creation unavailable`, no privilege); the junction variants ran. A POSIX run is `not_run` (CI).

### Mutants (scratch copy of `src`, `contracts`, `configs`, `prompts`; removed)

| Mutant | Killed by |
|---|---|
| `nofollow` (copy via `Path.read_bytes`) | `test_directory_link_refused_on_every_source_component[folder]` |
| `total_once` (`total = size`) | `test_unique_total_is_sum_of_distinct_sizes` |
| `intake_first_row_only` (`rows[:1]`) | `test_intake_refuses_damaged_retention_before_s4[unlisted_row]` |
| `percopy` (size/entry check `if False`) | `[unlisted_row]`; the size comparison alone (`size_compare_only`) by `[row_size_differs]` |
| `legacy` (marker compare `if False`) | `[other_layout]` |
| `pre_s4_read_skipped` (no read after S3) | `test_same_size_corruption_is_caught_after_s3_for_selected_images_only` |
| `missing_copy_code` (`RetainedFileError` to `StorageError`) | `[last_copy_removed]` |
| `pre_s4_refusal_dropped` (no refusal call) | pipeline test `[True]` |
| `table_vs_copies` (`!= retained and False`) | `test_publish_refuses_when_images_table_differs_from_retained_copies` |

No survivors. Earlier build mutants (`image_limit`, `total_limit`, `source_hash`, `source_size`, `coverage`, `returned_hash`) were killed in the build round.

### Deviations to know

- The brief expected the attachment hold to be gone after `after_delivery`. The code and `S6-envelope.md` keep the hold until the trace expires (the hold protects the snapshot while the trace is retained), so the pipeline test asserts: hold present at delivery and after `after_delivery`, request lease already released, hold released when the trace expires (`retention_s` 2 s, awaited, then `Pipeline.close()`).
- `tests/intake_fixtures.py::activate` gained an optional `renderer` argument (test fixture only) so a profile can accept reference images.
- Edit scripts under `.tmp/` wrote some tracked source files early in the round; the write guard later required the Edit tool, which I used from then on. Content is the same either way.
- Lint: `ruff check` and `ruff format --check` are `not_run`: `No module named ruff` in the project `.venv` (no install allowed). Tool suite `tools/agent-workflow/tests/` is `not_run` (no tooling change). No GPU, network, live model or install.

## Configuration audit

| Value or behaviour | Config / validation / profile | Override and refusal evidence | Class |
|---|---|---|---|
| Per-image byte limit | `sync.retained_images.max_image_bytes`; positive native int; base `deploy_required`; dev example 16777216 (dev-only) | `test_limits_refuse_without_changing_current`, `test_exact_limit_boundaries`, `test_invalid_limits_refused`, `test_each_limit_enters_sync_identity`, `test_dev_example_declares_explicit_retention_limits_and_refuses_bad_ones` | configurable |
| Unique total byte limit | `sync.retained_images.max_total_bytes`; same rules; dev example 1073741824 (dev-only) | same, plus `test_unique_total_is_sum_of_distinct_sizes`, `test_publish_unique_total_boundary_keeps_current_and_cleans_up` | configurable |
| Scanned extensions | `knowledge.image_extensions` | `test_extension_override_controls_retained_images`, `test_invalid_extensions_refused` | configurable |
| Store, data and knowledge paths | `storage.kb_store`, `storage.data_root`, `knowledge.root` | `test_kb_store_override_and_refusal`, `test_data_root_override_and_refusal`; link matrix: junction on folder, nested ancestor and source-root ancestor, snapshot `images/` directory (file-symlink variants skipped here) | configurable |
| Attachment selection | `edit.attach_kb_reference_images`, `edit.max_kb_reference_images`, `assembly.max_images_per_concept` | `test_assembly_reads_override_and_refusal`, `test_image_caps_are_read_only_when_attach_is_on`, the pipeline test, intake tests above | configurable |
| Vision transport | `vision.format`, `vision.max_image_side` | `test_vision_transport_does_not_change_retained_original` | configurable |
| Snapshot image path, marker name and content | one constant `RETAINED_IMAGE_MARKER`; `images/<sha256>` | `[other_layout]`, `[marker_removed]`, `[last_copy_removed]`, legacy tests | artifact-bound |
| Refusal text `image copies unavailable; run kb sync to republish`, code `KNOWLEDGE_FAILED` | `RETENTION_MESSAGE` (one constant) | `assert_kb_sync_refusal`, pipeline test `[True]` | protocol-bound |
| Hash and size checks, no-follow reads, bounded reads | fixed SHA-256 identity | mutants above; tamper, link and limit tests | safety-bound |
| Test-only literals (`1048576`, `10485760` in `tests/conftest.py`; the 2 s trace retention in the pipeline test) | labelled test values | not user values | test-only |

## Research evidence

| Question | Source | Finding | Class |
|---|---|---|---|
| Source read containment | `src/prompt_enhancer/k0_scan.py` `read_source_file`, `_opened` | opens every component from the filesystem anchor (so a link above the source root is also refused), checks inode before and after open, bounds the read | source-verified |
| Snapshot read | `storage/store.py` `PinnedSnapshot.read_file`, `retained_image_entry` | no-follow read, returned bytes hashed against the manifest | source-verified |
| Pin cost | `storage/store.py` `_verify_dir(full=False)` | stats every manifest file under the store lock, now one more per retained image | source-verified, not measured |
| Hold lifetime | `pipeline.retain_attachments`, `trace_store.py`, pipeline test | hold taken before lease release, released at trace expiry | experiment (test) |

## Open issues

- D4 (review-2 P3-C): `pin()` stats one more file per retained image under the store lock for every request and every `kb sync` unchanged check. Not changed. Measure pin latency at a realistic image count before deployment; if it matters, pin can skip `images/*` size checks because attachment use re-checks them (intake, after S3, S6).
- `S4-writer.md` and `writer.py:81` keep `attach` images out of the writer brief data; the `attach` per-image notes described in `S4-writer.md:10` are therefore not in the brief yet. Not part of KBS-F3; flag for the PM.
- Ruff, the POSIX run and the file-symlink variants need a machine that has them (CI).

## Handoff

- **Status:** ready for review. Nothing integrated, no Git writes.
- **Not to repeat:** the labelled doubles (model server, S2 plan, K3 stability, legacy publisher, damaged-table wrapper) are test doubles, not model evidence.
- **Cleanup:** `.tmp/KBS-F3-fix/` (basetemp, logs, mutant runner) removed; the pytest basetemp `.tmp/KBS-F3/pytest` was not used. No process left running.
