# P3-SWEEP-2: K4 error masking and the P3-SWEEP-1 follow-ups

Status: **implemented, tested, ready for review; nothing committed (the orchestrator commits).** Kind: code. R-IDs: R-001, R-004, R-009, R-014, R-015, R-017, R-026.
Interpreter: main tree `.venv/Scripts/python.exe`, `PYTHONPATH` = this worktree's `src`, every test through `test_slot.py`, `--basetemp .tmp/P3-SWEEP-2/pytest`, one pytest process at a time, never the full suite.
PM approved Step 0 on 2026-11-19 (Q1 probe first; Q2 changed to the owned-path / read-only split; Q3 to Q5 and item 6 as proposed).

## Step 0 (approved)

**What I read (source-verified):** `k4_aliases.py` (one `try` held the model call, `parse_alias_reply`, `_record_revisions`, `_generation_bundle`, `generated.commit`, and `except (ValueError, TypeError, UnicodeError, ModelCallError)` marked the concept failed); `k2_naming.py:59-70` (`failure_code` returns `SCHEMA_INVALID` for any error that is not a `ModelCallError`/`OSError` and has no known prefix); `kb_sync.py:88-100` (`_PER_CONCEPT_CODES` holds `SCHEMA_INVALID`, so `require_per_concept` tolerates it); `k0_sync.py:13-53`, `store.py` (`pin`, `_verify_dir`), `config.py` (`_resolve_within`, `_configured_path`, `_validate_k0_reads`), `tests/stage_guard.py`, `tools/agent-workflow/tests/test_ci_shard.py`.

**Unknowns and answers (PM-approved):** 1 recovery or no remedy for a damaged snapshot: probe first, remove the false remedy, no recovery path. 2 link rule: refuse a link only on owned or written paths; `knowledge.root` (read only, R-014) is resolved and its target validated, never refused for being a link; "PM decision pending user review". 3 `retain_images` split: cheap id/size/limit check stays before K1, read/hash/copy after the identity check. 4 K2 naming, observe, K3 narrowed only where the internal steps separate cleanly and no accepted test pins the old behaviour. 5 `k4_failed` gains `concept_ids_hash`; `kb-sync-4`.

## Root causes confirmed (each by reading the code, then by a failing test or a mutant)

1. **K4 `try` masks programming errors.** `failure_code` (`k2_naming.py:59-70`) maps an unprefixed `ValueError` to `SCHEMA_INVALID`; `_PER_CONCEPT_CODES` (`kb_sync.py:88-100`) tolerates `SCHEMA_INVALID` (decision 165); the old K4 `try` also covered `_record_revisions`, `_generation_bundle` and `generated.commit`. A defect in any of them was written as a failed checkpoint with a per-concept code, and `kb sync` published a snapshot without that concept's aliases. Proof: mutant M1 puts `_record_revisions` back in the `try` and the new test fails; the old code fails the same test.
   - Audit of the same pattern: **K2 observe** had `_rows` and `catalog.build` inside the `try` (same masking), now outside (`k2_observe.py:323-326`). **K2 naming** and **K3** hold the same shape but accepted tests pin it: `test_k3_fix_round1.py` (5 tests) and `test_k3_fix_round2.py::test_p1_reported_failure_cannot_erase_completed_reply` (4 tests) say a caught post-commit failure keeps the done reply and the retry reuses it, and for K1, K2, K3 `require_success` already blocks the publish, so a masked defect there yields a wrong failure code and a failed checkpoint, never a snapshot. I narrowed both, saw those 9 tests fail, restored the files from HEAD (`git diff` shows no change of the logic in `k2_naming.py` and `k3_cards.py`; they only gained `links=` below). Reported, not changed: "PM decision pending user review".
2. **A damaged current snapshot cannot be repaired by `kb sync`, but the messages promise it.** Probe (tests below): `kb sync` -> `k0_sync._previous_for_plan` -> `store.pin()` raises `RetainedFileError` for a missing or wrong-size listed copy before any build; a same-size corruption passes `pin` and fails `verify_snapshot` in the unchanged branch; so for damage the hint is false. It is true for a snapshot published before copies existed (no `images/retention-v1` row): a new sync republishes it (`test_legacy_current_is_republished_once_then_unchanged`). `RETENTION_MESSAGE` and `store.py` (`retained_image_entry`) said "run kb sync to republish" in both cases.
3. **A skipped identical publish still re-read, re-hashed and re-copied every retained image.** `retain_images` ran before K1 and did three jobs: validate id, size and limits (must fail before any model call), read and hash, copy. Proof: `test_a_skipped_identical_publish_copies_no_retained_image_and_the_real_publish_does` (a spy counts reads and copies; mutant M3 deletes the copy after the check).
4. **The identical-publish check could not tell two different failed sets apart.** `_publishes_the_same_snapshot` compared only `count` and `codes` of `k4_failed`: concept A failed then concept B failed (same count, same code) counted as identical and the snapshot kept the stale id list. Proof: `test_the_identity_check_needs_the_same_failures_and_the_same_alias_rows` (mutants M4, M5).
5. **The over-cap budget test passed for any server budget.** The `lingered` fixture computed `dropped` as `settings.max_discard_bytes - entry[1]`, so a server given a smaller starting budget still reported the full number; a client that reads at once cannot tell either. First measurement (mutants M9a, M9b, M9c with the late-reading client but the old record): all three survived, `1 passed`; the late reader alone does not separate them on Windows loopback (not even at budget 0).
6. **The spawn guard treated only a literal `[..., "-m", "pytest"]` as pytest.** Every other way to start a child (concatenation, spread, a command variable, a parameter, `shell=True`, `os.system`, `asyncio.create_subprocess_exec`, `from subprocess import run as go`) was invisible. A second defect surfaced while counting: a parameter named `env` was judged scrubbed because any function in the file assigned `env = child_environ()`.
7. **The loader followed in-root links on every path.** `_resolve_within` (`config.py`) followed a link that stayed under the root for `data_root`, `kb_store`, models, caches, `extends`, `overlay_of`, profiles and schemas; a written path reached its target through a link. AGENTS.md section 6 requires owned paths not to.
8. **`kb_audit` imported a private name.** `cli_commands.py` imported `_audit_snapshot`.

## Changes, per item

1. `k4_aliases.py:215-243`: the `try` holds the client/bound acquisition, `bound.call`, the `value is None` check and `parse_alias_reply`; the `except (ValueError, TypeError, UnicodeError, ModelCallError)` keeps the per-concept record (probe failure, `k4_call_failure` checkpoint, `generated.fail`, `report['failed']`) and returns. `_record_revisions`, `_generation_bundle` and `generated.commit` run after it, so an internal error propagates and `kb sync` fails (`test_a_k4_defect_fails_the_sync_instead_of_publishing_without_aliases`). The cached branch keeps its own `try` that turns a bad bundle into `CACHE_INVALID`. `k2_observe.py:323-326`: `_rows` and `catalog.build` outside the `try`. `K4_ALIAS_REVISION` unchanged: no function its pin guards was edited (`test_stage_revisions` 397 passed).
2. `intake.py:25-26`: `RETENTION_MESSAGE = 'image copies unavailable in the current snapshot'` (no remedy); `REPUBLISH_MESSAGE` keeps "run kb sync to republish" and is raised only when `store.PinnedSnapshot.has_retained_image_layout()` (`store.py:689`) is false, i.e. the snapshot has no `images/retention-v1` row, where a new sync does republish. `store.py:702` follows. `pipeline.py:75` (a copy that vanishes after S3, damage) uses the plain message. The one `grep "run kb sync"` neighbour, `cli_commands.py:44` ("run kb sync for the whole knowledge base"), is a different and true instruction and stays. No recovery path was added (see Noticed).
3. `kb_sync.py:37-65`: `retained_identities(rows, limits)` validates id, size and limits and returns the id set; it runs before K1 (`kb_sync.py:255`) and K1 compares against it. `retain_images` (read, hash, copy) runs after the identity check (`kb_sync.py:302`) and its result must equal the early set. The trade: a source file that changes during the model calls is found at the copy, not before K1.
4. `kb_sync.py:122-126` `_k4_failed_summary` returns `{count, codes, concept_ids_hash}` (sha256 of the sorted ids through `_digest`); it feeds both the stored `k4_failed` and the identity check, so a record without a hash (written before) compares unequal and the first sync after the upgrade republishes once. `KB_SYNC_REVISION = 'kb-sync-4'` (`kb_sync.py:33`), pin row `('kb-sync-4', '72f51ce3...')` appended to `tests/test_stage_revisions.py` (earlier rows untouched), inventory in `tests/stage_guard.py` gained `retained_identities` and `_k4_failed_summary`. `k4_failed` is in no contract file (grep). `docs/execution.md` and `docs/stages/K4-aliases.md` document the field, the identity check and the copy after it.
5. `tests/test_embed_server_lifecycle.py`: `refusal_exchange(..., before_reading=)` plus `read_after_the_server_ended_its_drain(lingered)`, a labelled late-reading client that waits for the server's own "drain ended" record (not a sleep alone) and then `LATE_READ_SETTLE_S = 0.3` (test-only). The `lingered` fixture now reads the budget from the server's own entry when the socket is kept and records `budget` and `dropped = budget - left`; the edge test asserts `[(budget, budget)]`. Because the late reader alone did not discriminate (root cause 5), the deterministic part is the record; the late reader stays as the realistic client.
6. `test_ci_shard.py`: `unscrubbed_pytest_spawns` reports every spawn that is not a static list unless its `env` is `child_environ()` (directly, through a variable of the same function or the module's top level, or a same-file helper); `os.system`/`os.popen` are never scrubbed. Module and alias tracking: `import m as x`, `from m import f as g`, `g = m.f` for subprocess, os, asyncio. `_visible_nodes` makes a parameter bound by no statement. 28 real spawns were flagged before exemptions, all start project tools, git or doubles, never pytest: `DELIBERATE_RAW_ENV` names 23 functions in 19 files, each with a reason, and a stale-exemption test fails if an entry no longer holds a reported spawn. 46 spawn shapes are tested in the two parametrized tests (counted by grep), including the three parameter shapes above.
7. `config.py`: `_resolve_within(..., links=False)` refuses a link at any component (`LinkedPath`, a `ConfigurationError`) unless `links=True`; the refusal order keeps the older, more specific facts first: resolved outside the root, then protected directory, then link. `_configured_path` and `_validate_configured_paths` pass `links=key == 'knowledge.root'`; `_resolved_knowledge_root` (links followed) feeds `_validate_k0_reads`; `validate_sync_config` additionally requires the resolved target to be a directory, only when retrieval is enabled (R-009). Stage re-resolutions of the knowledge root in `k2_naming.py:491`, `k2_observe.py:201`, `k3_cards.py:212` and (fix round 1) `k4_aliases.py:108` pass `links=True`; `storage/_fs.inside` passes `links=True` (its contract is "resolve after links"; `real_inside` is the refusing one and is unchanged); `cli_input.resolve_input` maps `LinkedPath` to its existing wording. Tests: `tests/test_config_links.py` (Hypothesis property over 1 to 3 segments, link position, kind `inside`/`owned`/`knowledge`, real junctions on Windows and symlinks elsewhere through `tests/link_fixtures.py`; a missing directory below a link; outside link; Windows case spelling; the real validator).
8. `kb_audit.audit_snapshot` (public); `cli_commands.py` follows; `test_kb_audit` asserts the old name is gone and no `src` module imports an underscore name from `kb_audit`.

## Accepted by the user (decision 174) and pending user review

- **Item 7 split (accepted by the user, decision 174):** owned or written paths refuse a link, also one that stays inside the root; `knowledge.root` follows it. A user whose model, cache or store folder is a link is now refused at load. Tests changed to the new rule: `test_fix01.py::test_internal_link_is_refused_for_a_stored_path_and_resolved_for_the_read_only_knowledge_root` (inverted), `test_fix02.py` (link to the project root now reports the link), `test_k0_review_fix.py` (a linked store reports the link; a linked knowledge root still overlaps), `test_kb_sync_gates.py` (spy takes `**options`).
- **Item 2:** no recovery path; the false remedy is gone; the true remedy (legacy layout) stays. A damaged `other_layout` marker now gets no hint (conservative).
- **Item 1:** K2 naming and K3 left as they are (9 accepted tests pin them; `require_success` blocks the publish).
- **Item 4:** the first sync after the upgrade republishes once.
- **Item 5:** deterministic check by the server's own budget record, the late reader informational.
- **Fix round 1 deviation (PM decision pending user review):** `kb_sync.sync_knowledge` passes `source.resolve()` as `knowledge_root` to K1 images, K2 naming and K2 observation (it passed the unresolved `project / knowledge['root']`). Reason: the QA review said a linked root synced before this change; run against the HEAD source (`base1`, `.tmp/P3-SWEEP-2/base1.summary`) the whole flow with a linked root fails at K1 with `k1_image: STAGE_FAILED: codes=SOURCE_CHANGED=1`, because K1 reads through a no-follow reader from the unresolved path. So the end-to-end claim was not true at HEAD, and the decision-174 allowance could not work through `kb sync` without this line. K0 still receives the configured path (`configured_root` keeps its meaning, `test_k0_review_fix.py` 42 passed); K2, K3 and K4 already compared `Path(knowledge_root).resolve()` with the resolved root. This changed a pinned function: `KB_SYNC_REVISION = 'kb-sync-5'`, pin row appended (`d8a0ee04...`); effect for users: the first sync after the upgrade re-plans once, as with every `kb-sync` bump.

## Pre-mortem (decision 169): how this change fails, and the check for each

| # | Failure | Check |
|---|---|---|
| 1 | Narrowed K4 `try` lets a real reply failure crash the stage (a per-concept failure turns into a stage failure, decision 165 broken) | `test_a_model_call_failure_of_one_concept_is_still_a_per_concept_failure`, `test_k4_failed_generation_has_failed_state`; mutant M2 (drop `generated.fail`) killed |
| 2 | An internal error is still tolerated | `test_a_defect_after_the_reply_fails_the_stage_and_is_not_recorded_as_a_failed_concept` (3 sites), `test_a_k4_defect_fails_the_sync_instead_of_publishing_without_aliases`, observe `_rows` test; M1 and M14 killed |
| 3 | Recovery or the message change reads damaged files, or the hint stays false | no recovery code added; `test_kb_sync_cannot_repair_a_damaged_current_snapshot_so_no_message_promises_it`, `test_the_republish_hint_is_given_only_for_a_snapshot_without_the_retention_layout`; M6 and M6b killed |
| 4 | Moved `retain_images`: a limit breach is found only after model calls, a copy is skipped when it must run, or the copied set differs from K1's | `test_a_scan_that_breaks_the_retention_limits_still_fails_before_any_model_call`, `test_a_skipped_identical_publish_copies_no_retained_image_and_the_real_publish_does`, equality check at `kb_sync.py:302`; M3 killed |
| 5 | The failed-id digest breaks a stored contract or leaks ids | grep: `k4_failed` is in no contract file; test asserts no ids are stored; the old record compares unequal (republish once, documented); M4 and M5 killed; `kb-sync-4` pin passes |
| 6 | Late-reader flakiness | the late reader waits on the server's record, not only a sleep; the deterministic assertion is the server's own budget; lifecycle file run 3 times before and 3 times after the last edit |
| 7 | The guard flags scrubbed spawns or misses new shapes | 46 shapes both ways; the real suites report 0 after 23 named exemptions; stale-exemption test; M10, M11, M12 killed |
| 8 | Link rule false positives: case spelling on Windows, a path that does not exist yet, POSIX | `test_another_case_of_an_existing_name_is_not_taken_for_a_link`, `test_a_link_through_a_directory_that_does_not_exist_yet_is_still_refused`, property test; POSIX not run here (`not_run`); the strict rule first broke tests in 9 files, 4 test files were updated and the rest passed after the refusal order was fixed |
| 9 | Disabled retrieval touches the knowledge root through the new directory check (R-009) | found by `test_disabled_sync_never_accesses_knowledge` while running the sweep, fixed by `config['rag']['enabled'] and ...`; M15 killed |
| 10 | Link rule weakens the read-only root's safety | `knowledge.root` still has to be inside the project, not `.secrets`/`models`, and a directory; outside link refused ('resolved outside project root') |
| 11 | Stage key changes without a revision bump | `test_stage_revisions` 397 passed; only `kb-sync-4` changed and its pin is appended |
| 12 | The mutant harness itself is wrong | first pass had no `contracts/` in the scratch copy and even a no-op control failed; fixed, controls C1 and C2 pass, all mutants rerun |

## Completion report

**Behaviour:** as in Changes per item. **Decision 170 (R-001, R-004, R-009, R-026), per item, none weakened:**

| Item | R-001 intent | R-004 admin prompt | R-009 retrieval off | R-026 data not instructions |
|---|---|---|---|---|
| 1 K4/K2 observe | no prompt text touched; a defect now stops instead of publishing without data | untouched (no prompt or system-prompt code) | `kb sync` with retrieval off still returns `skipped` before any call | replies are still parsed as data by the same parsers; no new reader of model text |
| 2 messages | unchanged | unchanged | n/a (retention read only when attachments exist) | messages name no content |
| 3 retain_images | unchanged | unchanged | skipped sync never reached | image bytes are copied, not read as text |
| 4 digest | unchanged | unchanged | n/a | the digest is of ids; no model text stored |
| 5 lifecycle test | test only | test only | test only | test only |
| 6 spawn guard | test only | test only | test only | test only |
| 7 link rule | unchanged | profile/overlay paths stricter, content untouched | retrieval-off path resolves nothing of the knowledge base; `test_disabled_sync_never_accesses_knowledge` passes (R-009) | knowledge text still read through the same no-follow reader |
| 8 rename | unchanged | unchanged | unchanged | unchanged |

R-014 (knowledge base read only): the `knowledge.root` link allowance is read only and is the one place a link is followed; the stored copies are written only to the project's own store. R-015: no new key (see audit). R-017: the first sync after upgrade is resumable; K4 checkpoints are reused.

**Commands and output (every run through `tools/agent-workflow/test_slot.py`, one file per process; `.tmp/P3-SWEEP-2/reports/`):**

- Sweep of the 82 test files that touch K0 to K5, kb sync, storage, config, profiles, intake, CLI, retention and the embed-server config: all pass (`b8.summary`: 83 lines, one failure in `test_kb_sync_gates.py`, a spy that rejected `links=`, and the R-009 slip of item 9; fixed, then `b10.summary`: `test_kb_sync_gates.py: 19 passed`, `test_config_links.py: 6 passed`).
- Final reruns after the last source edit (`fin.summary`): see the table below.
- `tools/agent-workflow/tests/test_ci_shard.py` alone: `125 passed in 53.67s`.
- Lifecycle file three times (`lcf1..3.summary`): see the table below.

| File (one process each, after the last source edit) | Result |
|---|---|
| `tests/test_stage_revisions.py` | 397 passed |
| `tests/test_kb_sync_alias_failure.py` | 29 passed |
| `tests/test_kb_sync_flow.py` | 25 passed |
| `tests/test_config_links.py` | 6 passed |
| `tests/test_k4_aliases.py` | 48 passed |
| `tests/test_k2_observe.py` | 37 passed |
| `tests/test_retained_images.py` | 71 passed, 2 skipped (symlink/POSIX cases) |
| `tests/test_embed_server_lifecycle.py` run 1, 2, 3 | 72 passed each |
| `tests/test_kb_sync_gates.py` | 19 passed |
| `tools/agent-workflow/tests/test_ci_shard.py` | 125 passed |

The `b8` sweep also covered, all passing: `test_k0_*` (6 files), `test_k1_*` (3), `test_k2_*` (all 20), `test_k3_*` (6, including `test_k3_fix_round1` 31 and `test_k3_fix_round2` 25), `test_k4_config`, `test_k4_properties`, `test_kb_audit` 13, `test_kb_sync_diagnostics`, `test_kb_sync_invalidation`, `test_kb_sync_production_probes`, `test_storage_*` (all), `test_cli_*`, `test_intake*`, `test_pipeline_s0_*`, `test_profile*`, `test_fix01..03`, `test_model_secrets`, `test_embed_server_manifest|reference|dev_compare`.

**Slip, disclosed:** three test edits (`test_fix01.py`, `test_fix02.py`, `test_k0_review_fix.py`) were first written to the main tree's path by mistake; I reverted them there and `git -C <main tree> status --short` is empty again. Nothing else outside this worktree was touched.

## Configuration impact and audit

No runtime configuration key was added, renamed or given a new default; no key's meaning changed. `rag.enabled`, `knowledge.root`, `storage.*` are read where they were read before. Row by row:

| Item | Literal or value | Class | Owner and test |
|---|---|---|---|
| 1 | `_PER_CONCEPT_CODES`, the caught exception tuple | protocol-bound | unchanged set; tests above |
| 2 | `RETENTION_MESSAGE`, `REPUBLISH_MESSAGE` text | protocol-bound (user-facing, pinned by tests) | `test_retained_images`, `test_pipeline_s0_s6` |
| 3 | retention limits | configurable (`sync.retained_images.*`, existing) | `test_a_scan_that_breaks_the_retention_limits_...` |
| 4 | `KB_SYNC_REVISION = 'kb-sync-5'` (4 for the digest, 5 for the resolved knowledge root, fix round 1); field name `concept_ids_hash`; sha256 | artifact-bound (stage key and stored summary) | pin test |
| 5 | `LATE_READ_SETTLE_S = 0.3` | test-only | lifecycle file |
| 6 | exemption table, `_SPAWN_FUNCTIONS` | test-only | `test_ci_shard.py` |
| 7 | `links=` argument (internal API, not a key); the `.secrets`/`models` sets | safety-bound | `test_config_links.py`, config tests |
| 8 | none | - | - |

Override and refusal tests exist only for user values; there is no new user value, so none were added (as expected in the brief).

## Mutants (scratch copies of `src` or `tools/agent-workflow`; controls C1 and C2, no edit, pass)

| Id | Change | Killed by |
|---|---|---|
| M1 | K4 `_record_revisions` back inside the `try` | `test_a_defect_after_the_reply_...[record_revisions]` |
| M2 | K4 drop `generated.fail` | `test_k4_failed_generation_has_failed_state`, per-concept call-failure test |
| M3 | kb_sync drops the copy after the identity check | `test_a_skipped_identical_publish_copies_...` |
| M4 | `concept_ids_hash` constant `''` | three alias-failure tests |
| M5 | identity compares only `count` and `codes` | `test_the_identity_check_needs_the_same_failures_...` |
| M6 | `has_retained_image_layout` always true | legacy-layout republish tests |
| M6b | `has_retained_image_layout` always false | five retained-image tests |
| M7 | `_resolve_within` default `links=True` | three `test_config_links` tests |
| M8 | `_resolved_knowledge_root` drops `links=True` | `test_the_sync_configuration_refuses_a_linked_store_and_accepts_a_linked_knowledge_root` |
| M9a, M9b, M9c | server budget `- 1`, `// 2`, `0` | `test_the_over_cap_budget_counts_the_request_head` (they survived with the late reader and the old record) |
| M10 | guard ignores a starred command | two spawn-guard shapes |
| M11 | guard drops `from`-import alias tracking | two shapes |
| M12 | guard ignores `shell=True` | one shape |
| M14 | observe `_rows` back in the `try` | `test_a_defect_while_building_rows_...` |
| M15 | `validate_sync_config` checks the directory with retrieval off | `test_disabled_sync_never_accesses_knowledge` |
| M16 | strict branch no longer raises | four config-link tests |

All 18 killed; none survived and none needed an explanation after the harness fix.

**Fix round 1 mutants** (scratch copies under `.tmp/P3-SWEEP-2/mut/<id>/`, run with that copy's `src` first on `PYTHONPATH`; `where.py` printed `...\mut\r1\src\prompt_enhancer\config.py`, so the mutant copy is the one loaded). Controls C1 (`config.py` no-op) and C2 (`k4_aliases.py` no-op) pass.

| Id | Change | Result |
|---|---|---|
| R1 | `config.py:189` refusal of another spelling turned into `if False:` | `test_config_links.py`: 1 failed, 9 passed; killed by `test_a_component_spelled_differently_from_its_real_name_is_refused_unless_links_are_followed` (control C1: 10 passed) |
| R2 | `k2_observe.py:326` `catalog.build` in `try/except (ValueError, TypeError)` marking the image failed | `test_k2_observe.py`: 1 failed, 37 passed; killed by `test_a_defect_while_registering_observation_rows_fails_the_stage_too` |
| R3 | `k2_naming.py:491` `links=True` dropped | `test_kb_sync_flow.py -k through_a_link`: 1 failed; killed by `test_a_sync_reads_the_knowledge_base_through_a_link_in_every_stage` |
| F1 | `k4_aliases.py:108` `links=key == 'knowledge.root'` dropped | the same test: 1 failed (control C2: 1 passed) |

## Fix round 1 (QA FEEDBACK, only round)

**Call-site audit** (every `_resolve_within(` and `_configured_path(` call that can receive `knowledge.root`):

| Call site | Passes `links=` for `knowledge.root` | Result |
|---|---|---|
| `config.py` `_configured_path` (generic loader, `links=key == 'knowledge.root'`) and `_validate_configured_paths` | yes | correct |
| `config.py` `_resolved_knowledge_root` (feeds `_validate_k0_reads`, `validate_sync_config`) | yes | correct |
| `k2_naming.py:491` | `links=True` | correct (R3 kills a regression) |
| `k2_observe.py:201` | `links=key == 'knowledge.root'` | correct |
| `k3_cards.py:212` | `links=key == 'knowledge.root'` | correct |
| `k4_aliases.py:108` (`_inputs.resolved`) | was missing | **fixed (F1)**; `_inputs` is in `LEFT_OUT`, so no K4 revision bump, `test_stage_revisions` still passes |
| `storage/_fs.inside` | `links=True` by contract | correct; `real_inside` refuses links and is unchanged |
| `cli_input.resolve_input` (profile, overlay, request paths) | strict, none of these is the knowledge root | correct |
| every other `_configured_path(...)` (models, cache, stores, data, verifier) | strict by design | correct (F7 rows) |

**Fixes:** F1 `k4_aliases.py:108`. F2 `test_a_sync_reads_the_knowledge_base_through_a_link_in_every_stage` in `tests/test_kb_sync_flow.py` (real junction on Windows, symlink elsewhere, skip only where neither can be made; `make_link` now asserts the result is a link through the new `is_directory_link`, so a test cannot pass on a plain directory; the whole `kb sync` flow runs: publishes, K2 observation, K3 and K4 call the model double, the concepts table is read back). F3 the refusal-of-another-spelling test (labelled `realpath` double). F4 `test_a_defect_while_registering_observation_rows_fails_the_stage_too`. F5 `alias_failures` docstring names `count`, `codes`, `concept_ids_hash`. F6 two sentences in `docs/execution.md` (the `kb sync` flow section). F7 `test_a_model_artifact_path_and_a_verifier_path_refuse_a_link[vlm.backends.main.model.file | validation.verifier.calibration_ref]` (the verifier key exists in `configs/enhancer.example.yaml`) plus `test_the_loader_refuses_a_linked_model_file_through_the_real_validator`. Item 7 relabelled "accepted by the user (decision 174)".

**Test results after the last edit** (each through `tools/agent-workflow/test_slot.py`, one file per process, fixed `--basetemp .tmp/P3-SWEEP-2/pytest`, size reported then deleted; `.tmp/P3-SWEEP-2/reports/fix1-final.summary`):

| File | Result | basetemp deleted |
|---|---|---|
| `tests/test_config_links.py` | 10 passed | 373 KiB |
| `tests/test_k4_aliases.py` | 48 passed | 671 KiB |
| `tests/test_k2_naming.py` | 26 passed | 740 KiB |
| `tests/test_k2_observe.py` | 38 passed | 891 KiB |
| `tests/test_kb_sync_gates.py` | 19 passed (R-009 gate intact) | 416 KiB |
| `tests/test_kb_sync_flow.py` | 26 passed | 5695 KiB |
| `tests/test_stage_revisions.py` | 397 passed (`kb-sync-5` pin appended, K4 unchanged) | 1841 KiB |
| `tests/test_k0_review_fix.py` | 42 passed | 552 KiB |
| `tests/test_kb_sync_alias_failure.py` | 29 passed | 2814 KiB |
| `tests/test_kb_sync_invalidation.py` (other `kb_sync` users, run after the `kb_sync.py` edit) | 26 passed | 3600 KiB |
| `tests/test_kb_sync_diagnostics.py` | 9 passed | 1006 KiB |
| `tests/test_kb_sync_production_probes.py` | 1 passed | 542 KiB |
| `tests/test_retained_images.py` | 71 passed, 2 skipped | 3911 KiB |
| `tests/test_fix01.py` | 60 passed, 1 skipped | 1147 KiB |

**Correction of the review's claim, with evidence:** the review said the same configuration synced before this change. It did not, end to end: with the HEAD source (`base1`) the F2 flow test fails with `StorageError: k1_image: STAGE_FAILED: count=1; codes=SOURCE_CHANGED=1`, because K1 reads from the unresolved link path. The F2 test therefore needed the one-line-per-stage change in `kb_sync.sync_knowledge` described under "Fix round 1 deviation" (`kb-sync-5`, pin appended). It is listed there as pending user review.

## not_run

- Lint: Ruff is not installed (`not_run`).
- POSIX/Linux run: this machine is Windows; the symlink branch of `tests/link_fixtures.py` and the POSIX case of every test were not executed (R-025 runs them in CI).
- The full suite and the CI shards: not run by rule; test files outside the swept set (serving, assembly, eval, planner and similar) and the other `tools/agent-workflow` suites were not run.
- After the last one-line `config.py` edit (retrieval-off guard in `validate_sync_config`) I reran only the files in the table above; the other files of the sweep passed before that edit, and no other caller of `validate_sync_config` exists (`kb_sync.py:211`).
- No live model, GPU, network or paid call; `.secrets/`, `data/`, `database/` untouched.
- Hypothesis ran 25 examples per property; a larger run is not done.
- Fix round 1: the POSIX symlink branch of the new flow test, the 8.3 short-name case of the "another spelling" refusal (the test uses a labelled `realpath` double because short names are off on many volumes) and lint (Ruff not installed) were not run. Files outside the list of the fix round (the wider sweep, `test_ci_shard.py`, the lifecycle file) were not rerun after the round: the round changed `kb_sync.py`, `k4_aliases.py`, `docs/execution.md` and test files only.

## Noticed, not done

1. **Recovery of a damaged current snapshot** (follow-up note, not built): `kb sync` fails at the scan's `store.pin()`. Sketch: let K0 treat `RetainedFileError` for retained copies only as "previous snapshot unusable for the plan", rebuild copies from the source (same hash-checked path), and publish over it with the mass-deletion guard decision stated; needs a PM decision (it changes what a pin may refuse).
2. The late-reading client does not separate budgets on Windows loopback; a Linux measurement could show whether the reset costs the reply there.
3. `cli_input.resolve_input`'s own string comparison now only catches a case-only spelling, since `_inside` refuses links itself.
4. K2 naming and K3 keep one broad `try` per item; narrowing needs the 9 accepted tests re-decided.
5. `DELIBERATE_RAW_ENV` has 23 entries; a shared `run_tool` helper that always scrubs would shrink it.
6. `tests/test_intake.py:444` still expects "kb sync" in the message of a legacy layout, which is true.
7. The `AUTO PROGRESS` block the PreCompact hook appended to this file was removed with the progress log.
8. (Fix round 1, reviewer note 1, deliberately not touched) POSIX `..` chain: a `..` component inside a path is followed by the OS before `_resolve_within` sees the components; a follow-up should normalise or refuse it on POSIX.
9. (Reviewer note) On Windows a volume mount point becomes a relative `Volume{GUID}\` path after `_strip_extended_prefix`, so it is refused as "outside the root" and not as a link; the outcome (refusal) is right, the wording is not.
10. (Reviewer note) The spawn guard in `test_ci_shard.py` treats `NAME = "pytest"` (a module-level string) as a static command; a spawn of a name held in a constant is not reported.

## Retest

Gemini Flash diff-only retest of fix round 1 (`P3-SWEEP-2-gemini-rt1`, PM-run): PASS, no P0 to P3. It confirmed every new `source.resolve()` runs after the retrieval-off skip (R-009), that the POSIX `..` exposure predates this change (K0 and `retain_images` already resolved the root), and that the `kb-sync-5` bump is required and sufficient.
