# K4-ALIASFAIL: a failed alias no longer blocks the publish

Status: **build round 1 (Claude Sonnet), implemented 2026-11-19; awaiting review.**

## Task brief

- **Goal (user decision 165):** a concept whose alias generation fails does not block the publish; the snapshot is published without that concept's aliases, the failure is recorded, and the next sync retries it.
- **Requirements:** R-015, R-017 (R-026: no model text in the record).
- **Out of scope:** alias inference settings (decision 166), every stage other than K4, `k4_aliases.py` (not edited, so `K4_ALIAS_REVISION` stays `k4-alias-2`).

## Step 0 (Small: yes)

Unknowns: none blocking. Proposed answers: exit status stays 0 (no documented rule says otherwise; `__main__.py:_legacy` returns 1 only for errors and for an over-guard `--plan`); the snapshot summary keeps `count` and `codes`, the ids go in the result. Plan: policy change in `kb_sync.sync_knowledge` only; new test file; revision bump and pin; docs.

## Design

- `kb_sync.sync_knowledge`: K4 no longer goes through `accept`/`require_success`. It records `reports['k4']` and its artifacts, computes `alias_failures(report['failed'])` and publishes. K1 image, K2 naming, K2 observation, K3 and K1 text still use `accept`.
- `alias_failures(failed)` returns `{count, codes, concept_ids}`: codes pass the closed filter `_closed_code` (the filter `require_success` already used, now one shared helper over `_CLOSED_CODES`, protocol-bound); `concept_ids` keeps only values that satisfy `is_concept_id`, sorted.
- Result of `kb sync` (printed as JSON by `__main__._legacy`, exit 0): `alias_failures` key, present only when a key failed. Snapshot `source_scan_summary` gets `k4_failed: {count, codes}` only when a key failed; `kb status` prints the summary verbatim (`cli_commands.py:59`), so operators see it there.
- Retry: the unchanged branch requires `'k4_failed' not in summary`. A rerun with the same inputs then proceeds; K0..K3 and done K4 keys are reused. Verified in `storage/store.py`: `done()` returns a row only for state `done` (`:1170-1186`), `reusable()` likewise (`:1189-1203`), so a `failed` key is re-called; `_transition` (`:1248-1257`) returns without writing when the previous row is `done`, so a failure never erases a completion and a done key is served from its checkpoint, never re-called.
- A key that still fails publishes again with the failure recorded; one call per failed key per run (`flow` has no retry loop).
- `KB_SYNC_REVISION` is `kb-sync-2`; pin row appended to `tests/test_stage_revisions.py` (digest from `stage_guard.stage_digest('kb_sync')`: `21917717a403616a36e52d92711f74c4502886ac4ca1ce537914a0bfdea24510`). The three new names are classified under `ERRORS` in `tests/stage_guard.py` (`require_success` was already there; it is left out of the pin, `sync_knowledge` is pinned).

## Answer to requirement (4): author and override aliases of a failed concept

Source-verified: `k4_aliases._alias_rows` (`:82-97`) builds each concept's rows from `candidates` (the model reply, absent for a failed key) and then, independently, from `effective[identity]['aliases']`, the admin's `kb_overrides.yaml` entries (`origin: author`, `model_revision: None`). `docs/stages/K4-aliases.md` says the same ("author wins ... null model/prompt provenance"). So author rows come from no model call; a model failure cannot remove them, and they stay published (test: `Name` keeps `('author', 'author')` and its one alias text embedding). Generated candidates only come from the model. No doubt remains, so no "PM decision pending" for this point.

## Pre-mortem (decision 169): failure classes and the test that rules each out

All tests in `tests/test_kb_sync_alias_failure.py` unless stated.

| Class | Test |
| --- | --- |
| Another stage's failure slips through | `test_a_k3_failure_still_blocks_the_publish`; `test_every_failed_stage_blocks_publication` (K4 removed from its list, five stages remain); `test_require_success_refuses_partial_rows`; mutant M1 |
| Stale aliases of the previous snapshot leak into the new one | `test_a_stale_generated_alias_of_the_previous_snapshot_does_not_leak` (old snapshot had `Name alias`; K4 key changes and fails; the new snapshot holds only the author row); mutant M3 |
| The retry never happens because the identity matches | `test_the_next_sync_retries_only_the_failed_key_and_publishes_when_it_succeeds` (same inputs, `published`, new snapshot id); mutant M2 |
| The retry re-calls done keys (R-017) | same test: `server.roles == ['alias_generator']` after the rerun; after success a third run is `unchanged` with zero calls; mutant M4 |
| Model text or a path leaks into the record | `test_the_recorded_failure_holds_only_concept_ids_and_closed_codes` (property, 60 generated failure maps with marker-prefixed codes and arbitrary keys); `test_a_failed_alias_publishes...` (marker absent from result and registry); `test_the_k4_failure_reaches_the_command...` (stdout/stderr); mutant M6. The failed-checkpoint error text is the closed code (`failure_code`), unchanged by this task |
| A still-failing key republishes forever without saying so | `test_a_key_that_keeps_failing_is_recorded_again_and_called_once_per_run`: every such run prints `alias_failures` and records `k4_failed`; not silent. It does publish a new, content-identical snapshot per rerun (decision 165 asks for "publishes again"); retention is bounded by `sync.snapshots.keep` |
| The result shape breaks `kb status`, `kb audit` or the command output | `test_a_failed_alias_leaves_the_status_summary_printable`; `test_the_k4_failure_reaches_the_command...` (exit 0, JSON parses). `kb status` copies `source_scan_summary` as JSON; `kb_audit.py` and `storage/tables.py` do not read its keys (JSON column); the new keys are additive |
| The recorded failure list is unbounded | `count` and `codes` in the summary are bounded by the closed code set; `concept_ids` is one id per failed concept, so bounded by the concept count of that snapshot (it appears in the result only, where the K4 report already lists the same ids) |

## Class sweep

- `require_success`: still called for k1_image, k2_naming, k2_observation, k3, k1_text (`kb_sync.py` `accept`); its callers in tests (`test_model_production_probes.py:463`, `test_probe_request_settings.py:586`) use k2_naming and are unaffected (the function's behaviour is identical, only the code filter moved into `_closed_code`).
- `report['failed']` of K4: read only by `sync_knowledge` now.
- `source_scan_summary` readers: `kb_sync.py` unchanged branch, `cli_commands.kb_status` (verbatim), registry write in `storage/store.py` (`finish`); no key-level reader elsewhere.
- Tests asserting `k4: STAGE_FAILED`: `test_kb_sync_flow.py` (partial-alias refusal removed, K4 dropped from the stage list), `test_kb_sync_diagnostics.py` (alias case removed from the STAGE_FAILED parametrization; K4's no-text guarantee is in the new file and the existing `test_k4_diagnostics_persist_codes_only`), `test_kb_sync_gates.py` (renamed, now uses k3).
- Docs: `docs/stages/K4-aliases.md` and `docs/execution.md` updated. `docs/stages/K5-index.md` states no all-or-nothing K4 rule (it only says the coordinator requires a validated K5 result), so it is unchanged.

## Completion report

Behaviour: as above. Commands (interpreter `.venv/Scripts/python.exe`, `PYTHONPATH=<worktree>/src`, through `test_slot.py`, one file per process, `--basetemp .tmp/K4-ALIASFAIL/pytest`):

- `tests/test_kb_sync_alias_failure.py`: 8 passed (15.6 s)
- `tests/test_stage_revisions.py`: 381 passed (44.6 s)
- `tests/test_kb_sync_flow.py`: 25 passed (73.6 s)
- `tests/test_kb_sync_diagnostics.py`: 9 passed (11.0 s)
- `tests/test_kb_sync_gates.py`: 19 passed (69.8 s)
- `tests/test_k4_aliases.py`: 44 passed (7.7 s)
- `tests/test_kb_sync_invalidation.py`: 26 passed (42.1 s)

Changed files: `src/prompt_enhancer/kb_sync.py`, `tests/stage_guard.py`, `tests/test_stage_revisions.py`, `tests/test_kb_sync_alias_failure.py` (new), `tests/test_kb_sync_flow.py`, `tests/test_kb_sync_diagnostics.py`, `tests/test_kb_sync_gates.py`, `docs/stages/K4-aliases.md`, `docs/execution.md`, this file.

Mutants (scratch copy of `src`, `contracts`, `configs`, `prompts`; test file above; baseline with no change: 8 passed, so the harness is valid): M1 K4 back to `require_success` killed; M2 unchanged branch ignores `k4_failed` killed; M3 stale generated row carried over for a failed key killed; M4 done key re-called (`cached = None`) killed; M5 summary drops the failure record killed; M6 raw code kept in the record killed (property test); M7 `alias_failures` not added to the result killed. No survivors.

PM decision pending user review: none required by requirement (4). One choice for the PM to confirm: the snapshot summary keeps only `count` and `codes`, and the failed concept ids are in the `kb sync` result only (to keep the registry small on a large knowledge base). Adding the ids to the summary is a one-line change if the PM wants `kb status` to list them.

## Configuration impact and audit (no new leaf)

| Row | Finding |
| --- | --- |
| New profile key | none; decision 165 is a fixed policy. No example-profile, override or refusal test is added or affected |
| Retained literals | `_CLOSED_CODES` (`MODEL_CODES` plus `SOURCE_CHANGED`, `SOURCE_READ_FAILED`): protocol-bound, previously inline in `require_success`; `'k4_failed'` summary key: artifact-bound (registry metadata); `KB_SYNC_REVISION`: artifact-bound |
| Hidden default / ignored setting | none: no value is read or defaulted |
| Hand-written content rule | none: codes are structural, ids are validated by `is_concept_id` |
| Configuration override and refusal tests | not run: no configuration read changed (`tests/test_kb_sync_gates.py` config tests are untouched) |

## not_run

- A live `kb sync` (no model call, no GPU, per the brief); behaviour over real models is unmeasured.
- `ruff` (not installed): lint and `ruff format --check` not run.
- Tool suite `tools/agent-workflow/tests/` (no tooling change); full pipeline suite; `test_model_production_probes.py` and `test_probe_request_settings.py` (they call `require_success('k2_naming', ...)`, unchanged behaviour); doc-budget check for the two edited docs.

## Noticed, not done

- Each rerun while a key keeps failing publishes a content-identical snapshot; if that churn is unwanted, a later decision could return `unchanged` when the retry fails identically. Left as decision 165 states.

## Review and PM integration fix (2026-11-19)

Claude Sonnet review `.tmp/K4-ALIASFAIL-rev/review-claude.md`: **PASS**, 8 of 8 mutants killed, one P2 and three P3.

| Finding | Action | Evidence |
|---|---|---|
| P2-1 run-level model codes (configuration, client state, capability, probe, budget) also became non-blocking | PM fix: `_PER_CONCEPT_CODES` (protocol-bound: `SCHEMA_INVALID`, `INCOMPLETE_OR_REFUSED`, `MODEL_CALL_INVALID`, `MODEL_CALL_FAILED`, `HTTP_ERROR`, `INPUT_INVALID`) and `require_per_concept`: any other code fails K4 as before. **PM decision pending user review:** the conservative reading of decision 165 ("a concept whose alias generation fails"); a failure that is the same for every concept, including the cloud budget the user granted, still blocks the publish | `test_a_run_level_alias_code_still_blocks_the_publish` (6 codes); mutant (check disabled) 6 failed |
| P3-3 `alias_failures` left out of the pin | pinned in the `kb_sync` stage with `require_per_concept` and `_PER_CONCEPT_CODES`; the `kb-sync-2` row digest is updated in place (`d37741db...`), because `kb-sync-2` was never released | `test_stage_revisions.py` 387 passed |
| P3-1 a key that keeps failing republishes a content-identical snapshot each run; after `sync.snapshots.keep` reruns the last snapshot with that concept's generated aliases leaves the keep-set | follow-up (size `keep`, or return `unchanged` on an identical failed retry) | review |
| P3-2 thinned coverage: no test that a reader pinned before a failing publish still reads the previous snapshot; no all-concepts-fail case | follow-up | review |

PM commands (`test_slot.py`, `PYTHONPATH` = worktree `src`): `test_kb_sync_alias_failure.py` 14 passed; `test_stage_revisions.py` 387 passed; `test_kb_sync_flow.py` 25 passed; `test_kb_sync_gates.py` 19 passed.
