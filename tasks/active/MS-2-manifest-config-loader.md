# MS-2: profile manifest v1 and enhancer config v2 loader (multi-source keys, limits and refusals)

Kind: code
R-IDs: R-002, R-004, R-012, R-015, R-001, R-009, R-026
Task file: tasks/active/MS-2-manifest-config-loader.md
Approach note: `.tmp/MS-2/MULTI-SOURCE-step0.md` (decisions 176, 177, 179, 184, 210)
Trivial: no. Small: no. Status: phase 3 done; see the Completion report.

## Step 0

### 1. Unknowns and failure modes (evidence read in this worktree, source-verified unless marked)

1. **A v0 manifest with attachments on works today.** The brief and the design say a v0 manifest loads "for single source with attachments off only". Four test fixtures build a v0 renderer with `accepts_reference_images: True` and `edit.attach_kb_reference_images: True` and run it green: `tests/cli_fixtures.py:146,155`, `tests/intake_fixtures.py:57-58`, `tests/test_intake.py:472,522`, `tests/test_pipeline_s0_s6.py:218`. Refusing that pair at MS-2 would refuse a pairing that loads today, which the pre-mortem forbids. No code reads `image_reference_syntax` (class sweep), so v0 attachments render no token today.
2. **Where a manifest and a config meet.** `config.load_config` never sees a profile. The pairing happens in `profiles._view` (`profiles.py:532-533`), called by `check_profile` (`profiles.py:771`) and by `resolve_profile` (`profiles.py:899`, once per request in S0). Decision 177's refusal already lives there (`_require_attachable_renderer`, `profiles.py:521-529`). So "load" for a manifest rule means: `profile check` and each request's profile resolution. A rule about the manifest alone sits in `config.load_profile` (`config.py:2625-2648`).
3. **`vlm_role.v0` forbids extra keys** (`additionalProperties: false`, `contracts/schemas/vlm_role.v0.schema.json:7`) and eight call sites validate a role against it (`config.py:1555,1628,1711,1781,2014,2057,2373,2541`). A role that carries `multi_source` fails there unless the key is split off first. `vlm_role.v0` must stay byte-identical.
4. **Reads validate against `enhancer_config.v0`, not the versioned schema.** `validate_planner_reads` (`config.py:2003`) and `validate_writer_reads` (`config.py:2047`, which validates `vlm.backends.<b>` whole at `:2087`) call `_schema_at` on the v0 file, where a backend object is closed (`additionalProperties: false`). A v3 backend with `image.max_images_per_call` fails there. Both must take the versioned schema (`_configuration_schema`, `config.py:2109`).
5. **Config hash and probe key move for a v3 config.** `intake.py:196-201` hashes the whole effective config into `config_hash`, and `model_client.py:379` puts the whole `backend['image']` into the role probe context key. A v3 config therefore hashes differently and re-probes once, because the config gained keys. A v2 config loaded as is keeps both, provided the loader injects nothing. `model_client.py` is MS-4's file, so this is recorded and the file stays untouched. The request hash (`model_client.py:951`) reads only `backend['image']['max_side']` and does not move.
6. **A request override could name a limit.** `request_overrides.allowed_keys` takes any existing leaf path (`config.py:1945-1960`). Examples keep it empty (`enhancer.example.yaml:187`, `enhancer.dev.example.yaml:74`). `effective_request_config` re-runs the whole load check on the effective config (`config.py:2487` via `:2486`), so a raised `edit.max_source_images` above `max_images_per_call` is refused again there.
7. **The drafter can answer `null`.** `manifest_drafter_v1.txt:14-16` (MS-1) says `null` when the prompt does not state a renderer value. `register_profile` validates the reply against the manifest schema and refuses to write an invalid draft (`profiles.py:322-336`), and it pins `schema_version: 1` and the schema id `profile_manifest.v0` in code (`profiles.py:293,304`).
8. **The pilot profile.** `runs/T-017-pilot/profiles/pilot-v1/manifest.yaml` is a copy of `_example` (v0 manifest, `accepts_reference_images: false`, `image_reference_syntax: none`, read from `runs/T-017-pilot/jobs/J3/manifest.drafted.yaml:19-20`; the bundle file itself is not opened or edited). Its pilot configs extend `configs/enhancer.dev.example.yaml` with `schema_version: 1` (`runs/T-017-pilot/profile-pilot.yaml:5-6`).
9. **Fixture breadth.** 27 test files read the example configs, so they inherit v3 once the examples move. A few build inline `schema_version: 2` configs (`tests/test_addendum_layout_config.py:211`, `tests/test_cli_inputs.py:143,314,317`, `tests/validation_fixtures.py:49`); `tests/test_loader.py:444` expects a manifest `schema_version: 2` to be refused, which becomes false.
10. **The step budget.** The suite files named in the brief ran 287 s in MS-1; with fixture repair I expect about 80 to 100 calls. I propose two phases (below).

### 2. Questions (ranked by impact; each with a proposed answer)

1. **v0 manifest, attachments on.** Proposed: MS-2 refuses a v0 manifest only when the paired config has `edit.max_source_images` of 2 or more (message names `profile register`). A v0 manifest with attachments on keeps loading, as today (unknown 1). Its refusal moves to MS-5, the first stage that renders a `ref` token, which v0 cannot name (it has one syntax for both groups). If you want the refusal now, it is one more line in `_view` and about six fixtures change to a v1 renderer; say so and I do it.
   - Checkable at MS-2: v0 with config cap at least 2, at `profile check` and each request's profile resolution (`_view`).
   - Waits for MS-3: a request with n of 2 or more against the effective cap. The effective cap there is `min(edit.max_source_images, manifest cap)`, with the manifest cap of a v0 manifest being 1. With cap 1 the MS-3 count check already refuses n of 2 or more.
2. **Config `schema_version: 2` for request commands.** Proposed: a v2 config stays loadable for every command that reads none of the new keys: `kb sync|status|rollback|gc|audit|show`, `embed serve|reference-check|dev-compare`, `eval build|import|fit`, `profile register` (their `COMMAND_READS`, `config.py:49-100`, name no `edit`, `vlm.roles.planner|writer` or `vlm.backends.<b>.image` for requests). The request commands `enhance once`, `enhance serve`, `eval run` and the golden run of `profile check` (`cli_commands.py:135-141` loads `enhance once`) read `edit` and the planner and writer roles, so they refuse v2 with a message that says to migrate to `schema_version: 3` and names the new keys, as `_require_validation_version` does for v1 (`config.py:2334-2337`). The alternative (accept v2 as "cap 1") would be a version-defined default, which AGENTS.md section 5 forbids. Cost: a standalone v2 deployment file needs the new keys before it serves; a file that `extends` the repo examples gets them from the example (own `schema_version` below the base's is promoted, as `config.py:748-750` does today).
3. **Drafter reply shape.** Proposed: pin it. `profile_manifest.v1` allows `null` for the three drafted renderer values (`max_source_images`, `source_image_syntax`, `reference_image_syntax`), the same convention v0 uses for `image_reference_syntax`, so `register` writes the draft instead of losing the paid call. `load_profile` then refuses `null` in each, naming the key and saying the admin states it. The drafter prompt text stays as MS-1 wrote it (no diff). `register_profile` takes the manifest version from the role's `output.schema_id` (`profile_manifest.v0` gives `schema_version: 1`, `profile_manifest.v1` gives `2`), so the examples keep the v0 drafter and nothing changes for today's `register`; the v1 drafter is used when a config selects it. Not done: a separate reply schema (a new contract file outside the brief's list).
4. **Template rule.** Proposed, structural only: a syntax is `none` or a string with exactly one `{n}` and no other `{` or `}` character, so `{N}`, `{ n }`, `{{n}}`, `{n}{n}` and `a{n}}` are refused (an escaped brace cannot pass). Renderings are `template.replace('{n}', str(i))` for i in 1..N; "overlap" is equality of a source rendering and a reference rendering. At manifest load N is `renderer.max_source_images`. At the pairing in `_view`, when attachments are on and `edit.max_kb_reference_images` is an integer, N is the larger of the two, because the reference count is not capped by the manifest. A prefix such as `ref 1` against `ref 10` is not equality and is not refused (a bracketed syntax avoids it); I list it under `Noticed, not done`.
5. **`_example` moves to a v1 manifest now.** Proposed: yes, with the design's values (`source_image_syntax: none`, `reference_image_syntax: none`, `max_source_images: 1`, `accepts_reference_images: false`, `schema_version: 2`), because brief item 4 names it and the design's worked example 1 uses it. Effect: `profile check` of a bundle copied from `_example` runs on v1 (the activation binds the new manifest bytes, so a bundle copied earlier is not "re-activated" by this change; its own manifest is untouched). `pilot-v1` stays a v0 manifest and keeps loading under MS-2 (config cap 1 and attachments off), with no edit to `runs/`. `GoldenAdapter.identity` does not change at MS-2, so no activation under `runs/` is invalidated by this task. Tests that need a v0 manifest keep their own v0 fixture (`tests/profile_fixtures.py`), and the v0 loader is tested directly.

### 3. Plan (finish checks map to the done check)

Phase 1 (loader, schemas, tests): steps 1 to 6. Phase 2 (docs, mutants, report): steps 7 to 9. I report at the end of phase 1 only if the budget is at risk.

1. Guard first: a test with the SHA-256 of every existing `*.v0.schema.json`, `enhancer_config.v1.schema.json`, `profile_manifest.v0` and the other v1/v2 files as they are today, so any in-place edit fails.
2. `contracts/schemas/profile_manifest.v1.schema.json` (copy of v0 with the new `renderer`, `schema_version` const 2). `contracts/schemas/enhancer_config.v2.schema.json` (`schema_version` const 3; unchanged keys by `$ref`; `edit` inline with `max_source_images`; the three `vlm` nodes as small fragments that `_configuration_schema` composes, as it already composes `runtime` from `pipeline_runtime.v0` at `config.py:2125-2128`; a test proves the composed schema equals the v1 expansion plus exactly those deltas).
3. `config.py`: accept `schema_version` 3 (`:731,1910,2113`); promote an own version below the base's (`:748,1926`); `_require_source_limits_version` for request commands (Q2); one validator per refusal of the brief's list; `_role_core` strips `multi_source` before the eight `vlm_role.v0` checks; planner and writer reads use the versioned schema; `load_profile` dispatches on manifest `schema_version` 1 or 2 and refuses null and the template cases; the shared limit and template validators live in one place and are what the property tests call.
4. `profiles.py`: `_view` adds the v0-with-cap-2 refusal and the pairing overlap check; `register_profile` takes the version from the role's schema id (Q3).
5. Examples: `enhancer.example.yaml` (`schema_version: 3`, base `deploy_required`), `enhancer.dev.example.yaml` and `enhancer.dev-qwen3vl2b.example.yaml` (dev `1`, `max_images_per_call` per backend with the dev value), `configs/profiles/_example/manifest.yaml`. `request_overrides.allowed_keys` stays `[]`.
6. Tests: new `tests/test_ms2_multi_source_config.py` and `tests/test_ms2_manifest_v1.py` (override and refusal per key, property tests, v0 and v2 stay loadable, one-image bytes unchanged, retrieval off, allowed-keys), then repair of the fixtures that read what changed (grep `tests/` for each changed message and function).
7. Docs: `docs/stages/P-profiles.md`, `configs/profiles/_example/README.md`, `contracts/README.md` (versions loaded now; "designed, not yet implemented" kept for MS-3 to MS-7).
8. Mutants (3 to 10) on a scratch copy; each named killed or explained.
9. Run the focused sets through `test_slot.py` (one file per process), fill the Completion report, Configuration impact and audit, remove `.tmp/MS-2/` scratch.

Finish checks: the brief's test sets green through `test_slot.py`; the hash guard; the one-image byte test with a v0 and a v1 manifest; mutants reported; Completion report with the row-by-row audit.

## PM approval of Step 0 and changes (read in phase 1)

Q1 accepted (v0 refused only with `edit.max_source_images` >= 2; attachments-on refusal moves to MS-5). Q2 accepted, plus every tracked example config at 3 and a read-only run on `runs/T-017-pilot/profile-pilot.yaml`. Q4 changed: overlap is equality or substring within a group and across groups, for n from 1 to the cap. New: refuse at load an `allowed_keys` entry naming a limit. Q3 and Q5 accepted. Addendum: bind the activation's schema snapshot to the manifest's version; state what a child at 1 or 2 of a version-3 base becomes; every request command accepts 3 and gives the migrate message for 2.

## Steps (progress log)

1. Wrote `contracts/schemas/profile_manifest.v1.schema.json` and `enhancer_config.v2.schema.json` (new files only).
2. `config.py`: versions 1..3 accepted; a child below the base's version is promoted to the base's (chain and overlay); `_configuration_schema` composes v2 over v1 (`_extend_source_limits`); `_require_source_limits_version`; `check_limit`, `syntax_error`, `render_syntax`, `overlapping_syntaxes`, `validate_renderer`; `validate_source_limits` (after the planner and writer reads); `_role_core`; `_reads_schema`; the `allowed_keys` limit refusal; `load_profile` dispatch on manifest 1 or 2.
3. `profiles.py`: activation schema snapshot bound to the manifest's own version (`check_schemas`); `SourceLimitManifestMismatch` (subclass of `AttachmentRendererMismatch`, so intake passes its fixed text); `_require_source_pairing` in `_view`; `register_profile` takes the manifest version from the drafter role's `output.schema_id`.
4. Open: examples, tests, docs, mutants. Note: `CHECK_CODE_SHA256` (`profiles.py`, end of file) hashes `config.py`'s AST and `profiles.py`'s bytes, so any edit of either moves it and every existing activation reports `PROFILE_NOT_CHECKED` (check code differs). That is decision 124's design, not avoidable here; the schema binding itself stays the same four ids for a v0 manifest.

## Class sweep (read before code; file:line from this worktree)

**Readers of the manifest `renderer` block**

| Where | What it reads |
| --- | --- |
| `contracts/schemas/profile_manifest.v0.schema.json:121-143` | the block's shape (`accepts_reference_images`, `image_reference_syntax` string or null) |
| `src/prompt_enhancer/pipeline.py:146` | `renderer.accepts_reference_images` (attachments only for edit) |
| `src/prompt_enhancer/writer.py:93` | `renderer.accepts_reference_images`, raises if false with attachments |
| `src/prompt_enhancer/profiles.py:526` | `renderer.accepts_reference_images` in `_require_attachable_renderer`, called by `_view` at `:533` |
| `src/prompt_enhancer/profiles.py:293,304,322` | drafter schema id, `schema_version: 1`, reply validation |
| `configs/profiles/_example/manifest.yaml:21-23`, `prompts/manifest_drafter_v0.txt:13`, `docs/stages/S4-writer.md` | the example and the drafter text name `image_reference_syntax`; no code reads it |
| tests: `tests/profile_fixtures.py:75`, `cli_fixtures.py:155`, `intake_fixtures.py:58`, `test_intake.py:522`, `test_pipeline_s0_s6.py:218`, `test_retained_images.py:267`, `test_trace_store.py:51`, `test_profiles.py`, `test_writer.py` | v0 renderer dicts |

**Readers of `schema_version`, config**: `config.py:731` and `:748-750` (chain), `:1910` and `:1926-1927` (overlay), `:2113-2115` (`_configuration_schema`), `:2336` (`_require_validation_version`), `:2449,2494` (error naming). `intake.py:196-201` hashes the whole effective config. **Manifest**: `config.py:2635-2640` (`load_profile`, expects 1, schema `profile_manifest.v0`), `profiles.py:304` (code-owned `schema_version: 1`), `profiles.py:623-625` (golden retired version, not the manifest).

**Readers of `vlm.backends.<b>.image`**: `config.py:1202,1363` (embedder and server read lists), `:1353`, `:1579`, `:1638` (K roles `image.max_side`), `:2017` (planner `image.max_side`); `k1_embed.py:113` (`max_decode_pixels`); `model_client.py:122,151-152,183-185,379,892,951`; `model_probe.py:221`; the deployment overlay allow-list names `image` (`config.py:~768`).

**Readers of `edit`**: every request command reads the whole block (`COMMAND_READS`, `config.py:76-100`); `config.py:2037` (`source_entity_linking`), `:2137-2144` (`_require_attachment_support`), `:2407` (`ASSEMBLY_ATTACH_READS`).

## Phase 1 to phase 2 handoff (3 lines)

1. Phase 1 done: schemas `profile_manifest.v1` and `enhancer_config.v2`, loader (`config.py`), pairing and activation binding (`profiles.py`), the four examples; the shape of the new refusals is fixed by `validate_renderer`, `validate_source_limits` and `_validate_override_policy`.
2. Phase 2 = tests (`test_ms2_config_v2.py`, `test_ms2_manifest_v1.py`), fixtures (`conftest.py`, `profile_fixtures.py` carry the new keys with test-only values), docs, mutants, this report.
3. Two defects found by the new tests and fixed in the loader: the writer template check accepted a doubled `{{data}}` (`placeholders()` lists each name once), and `SourceLimitManifestMismatch` called a no-argument initializer with an argument.

## Pre-mortem (decision 169): how this change could fail, and the test that would show it

| # | Failure | Guard (test) |
| --- | --- | --- |
| 1 | A shipped schema is edited in place (hash guard) | `test_existing_schema_files_are_never_edited_in_place` (37 files: every `*.v0.schema.json` and `enhancer_config.v1`), `test_the_guard_lists_every_v0_schema_and_enhancer_config_v1`; `git diff --stat HEAD -- contracts` shows only two new files |
| 2 | A v0 manifest or a v2 configuration is refused or reinterpreted | `test_a_version_1_manifest_stays_loadable`; `test_a_version_2_configuration_still_loads_for_commands_that_read_none_of_the_keys` (dict unchanged); `test_every_request_command_gives_the_migrate_message_for_version_2` (3 commands); `test_no_request_command_refuses_version_3`; a v0 manifest with cap 2 is refused with a fixed message (`test_a_version_1_manifest_with_a_cap_of_two_or_more_...`); cap 1 passes |
| 3 | A silent default, or a sentinel read as a value | `test_edit_max_source_images_is_refused_by_key` (sentinel, 0, -1, True, 2.0, '2', null), `test_a_missing_limit_is_refused_not_defaulted`, `test_a_missing_multi_source_block_is_refused`, `test_a_backend_limit_equal_to_the_cap_passes_and_a_sentinel_backend_limit_is_refused`, manifest nulls (`test_a_null_the_drafter_left_is_refused_naming_the_key`) |
| 4 | Template near-misses (`{n}` twice, `{N}`, `{ n }`, escaped brace, collisions) | `test_template_near_misses_are_refused_naming_the_key` (16 x 2), `test_overlap_is_equality_or_substring_within_and_across_groups`, the property test `test_the_template_validator_accepts_exactly_one_n_and_substring_free_renderings` against an independent brute-force oracle (300 examples) |
| 5 | A request raises a limit | `test_a_limit_in_allowed_keys_is_refused_at_load_naming_the_key` (3 key forms plus `renderer.max_source_images`), `test_a_request_override_naming_a_limit_is_refused_with_the_default_policy`; every example keeps `allowed_keys: []` |
| 6 | One-image bytes change | `test_a_one_image_request_gives_the_same_bytes_with_a_version_1_or_a_version_2_manifest`: `render_brief` output for a v0 and a v1 renderer is equal and equals the pinned SHA-256 `934265d5...be59` (measured with `writer.py` unchanged from HEAD: `git diff --stat HEAD -- src/prompt_enhancer/writer.py` is empty); `request.v0` is untouched |
| 7 | Retrieval off reads the knowledge base (R-009) | `test_retrieval_off_reads_no_knowledge_base_for_the_limits` (the path helpers raise if called); no new code path opens a knowledge base |
| 8 | Knowledge-base or image text acts as an instruction (R-026) | this task loads and validates keys only; no model call, no knowledge-base or image text is read; the new refusal text is fixed (`SourceLimitManifestMismatch.MESSAGE` has no configured value, profile id or path) |
| 9 | The config or manifest hash changes without a recorded reason | recorded below: `config_hash` and the probe key change for a version-3 configuration (new keys inside `edit` and `backend['image']`); `CHECK_CODE_SHA256` moves because `config.py` and `profiles.py` changed |
| 10 | Activation snapshot bound to the wrong manifest version (PM addendum 1) | `test_the_schema_snapshot_is_bound_to_the_manifest_version`, `test_a_version_1_profile_still_checks_and_resolves_with_its_original_schema_set` (record holds the original four ids), `test_register_with_the_version_2_drafter_...` (record holds `profile_manifest.v1`) |
| 11 | A child at version 1 or 2 of a version-3 base (PM addendum 2) | `test_a_child_below_its_base_takes_the_base_version` (6 cases), `test_a_pilot_style_child_of_the_dev_example_becomes_version_3`: the child takes the base's version and so its keys; a child that states a lower version than its base is not refused |

## Noticed, not done

- **MS-5 obligation (Q1).** A v0 manifest with `edit.attach_kb_reference_images: true` still loads today (pinned by `test_a_version_1_manifest_with_attachments_on_still_loads_MS5_obligation`). MS-5 must refuse it. The design sentence: "A v0 manifest still loads, for single-source requests with attachments off only, because that is exactly what it means. Anything else is refused with a message naming `profile register`."
- **MS-3:** the effective cap is `min(edit.max_source_images, renderer.max_source_images, backend image.max_images_per_call)` and it must be computed at intake; MS-2 validates each limit separately and checks only `edit.max_source_images` against the planner's and writer's backends. The per-call cap in the model client is MS-4 (`model_client.py` is untouched).
- **Cost note:** `overlapping_syntaxes` is linear in the cap times the square of the template length; a very large `max_source_images` (up to `sys.maxsize` is allowed) makes the overlap check slow and large. No upper bound is invented here (R-015); the PM decides whether a `safety-bound` ceiling is wanted.
- **Hashes (PM, Q3 and Q5):** `intake.py:201` hashes the effective config and `model_client.py:379` hashes `backend['image']` into the probe key, so a version-3 configuration gives a new `config_hash` and a re-probe; neither file is touched. `CHECK_CODE_SHA256` moves with any edit of `config.py` or `profiles.py`, so every existing activation, including the pilot's, reports `PROFILE_NOT_CHECKED` until `profile check` is run again (decision 124).
- `intake` passes `SourceLimitManifestMismatch` through its redacting boundary because it subclasses `AttachmentRendererMismatch` (`intake.py:149`); no intake change.
- The drafter reply shape: `profile_manifest.v1` has no `minimum` or `pattern`, so it can be a structured-output schema; the drafter writes `null` for what the prompt does not state and the loader refuses the null at `profile check` and `confirm` (tests above). `prompts/manifest_drafter_v1.txt` is unchanged (out of scope); `prompts/manifest_drafter_v0.txt` still names `image_reference_syntax` (v0 contract).
- `enhancer.dev.example.yaml` states `max_images_per_call: 1` as an assumption (the llama-server flag that sets its own limit was not confirmed here, marked [A] in the file).
- `ruff` is not installed in the project `.venv` (`not_run`).

## Pilot report (read-only, `runs/T-017-pilot/profile-pilot.yaml`, not edited)

The pilot file states `schema_version: 1` and extends the dev example, so it is promoted to 3. `kb sync`, `profile register` and the first `profile check` load. `enhance once` (and the second load inside `profile check`) is refused with `vlm.backends.cloud_claude.image.max_images_per_call: unresolved sentinel deploy_required`. The one key the PM must add: `vlm.backends.cloud_claude.image.max_images_per_call` (a copy with the value 1 loaded for both `enhance once` and `profile check`). Its activation also reports `PROFILE_NOT_CHECKED` until re-checked (code fingerprint changed).

## Research evidence

- Source-verified (this worktree): `config.py` (chain and overlay promotion, `_configuration_schema`, `_extend_source_limits`, `validate_source_limits`, `validate_renderer`, `_validate_override_policy`), `profiles.py` (`check_schemas`, `_require_source_pairing`, `register_profile`, `CHECK_CODE_SHA256`), `writer.py:69-100` (reads `accepts_reference_images` only), `intake.py:149,196-201`, `model_client.py:379`.
- Experiment: the pilot load above (scratch copy under `runs/`, removed), the test runs below.
- Assumption: the dev backends carry one image per call (marked in the file).

## Configuration impact (filled before the runtime edit)

New keys: `edit.max_source_images`, `vlm.backends.<b>.image.max_images_per_call`, `vlm.roles.planner.multi_source.{prompt_id, schema_id}`, `vlm.roles.writer.multi_source.prompt_id`, manifest `renderer.{max_source_images, source_image_syntax, reference_image_syntax}`. All are validated profile or manifest values with no code default; the base example holds `deploy_required` for the limits; a missing key is refused at load naming it.

## Configuration audit (row by row)

| Row | Example | Override test | Refusal test | Literal class |
| --- | --- | --- | --- | --- |
| `edit.max_source_images` | base `deploy_required`, dev `1` (`test_example_values_follow_the_design`) | `test_override_two_images_with_a_higher_backend_limit_and_other_prompts` (cap 2, 5) | `test_edit_max_source_images_is_refused_by_key`, `test_a_missing_limit_is_refused_not_defaulted`, property test | none retained (value) |
| `vlm.backends.<b>.image.max_images_per_call` | base `deploy_required`, dev `1` | same test (backend 4, 5) | `test_a_backend_limit_below_the_cap_is_refused_for_the_role_backend` (planner, writer), sentinel and missing cases | none retained |
| `vlm.roles.planner.multi_source.prompt_id` | `planner_v2` | override with a temp prompt id | `test_an_unknown_multi_source_prompt_id_is_refused`, sentinel | none retained |
| `vlm.roles.planner.multi_source.schema_id` | `planner_reply.v1` | n/a (one allowed value) | `test_the_planner_schema_id_must_be_planner_reply_v1` | `PLANNER_MULTI_SOURCE_SCHEMA = 'planner_reply.v1'`: protocol-bound |
| `vlm.roles.writer.multi_source.prompt_id` | `brief_v1` | override with a temp brief | unknown id; `test_a_writer_template_needs_exactly_one_data_variable` (0, 2, other) | `{{data}}`: protocol-bound |
| manifest `renderer.max_source_images` | example `1` | `test_non_default_values_are_accepted` (3, 1024) | `test_a_bad_source_image_limit_is_refused`, null, missing | none retained |
| manifest `renderer.source_image_syntax`, `reference_image_syntax` | example `none` | `<source_{n}>` / `<ref_{n}>` accepted | near-miss, equality, overlap, `none` with cap above 1, reference with `accepts_reference_images` false | `{n}` (`IMAGE_SYNTAX_FIELD`): protocol-bound; `none` (`NONE`): protocol-bound |
| configuration version gate | all examples at 3 | version 3 accepted for the 3 request commands | version 2 refused with the migrate message for the 3 request commands; unknown version refused | `CONFIG_VERSIONS`, `SOURCE_LIMITS_VERSION`, `MANIFEST_VERSIONS`: artifact-bound |
| `request_overrides.allowed_keys` | `[]` in every example | `test_other_keys_in_allowed_keys_still_pass_and_the_matcher_is_exact` | 4 limit-key forms refused at load | `_is_source_limit_key` matcher: safety-bound (the unused `SOURCE_LIMIT_KEYS` tuple was removed in fix round 1) |

No retained literal is a `defect`; no hidden default; no word list or natural-language pattern (the checks are structural: one `{n}`, brace counts, substring containment of renderings, integer type and range).

## Completion report (phase 3)

**Status.** Every file that reads configuration or profiles passes on this worktree through `test_slot.py` (one file per process), except two files whose failures are not caused by MS-2 (see Findings 4). No assertion was weakened; no refusal message changed. The rest of `tests/` is in the table with its result; a file marked `not_run` was still queued when the budget ended.

**Behaviour.** Unchanged from phase 2 (loader, schemas, examples). Phase 3 changed tests only; `config.py` and `profiles.py` were not touched (no loader defect was found).

**Commands.** For each file: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree>/src <venv>/Scripts/python.exe -B -X faulthandler tools/agent-workflow/test_slot.py -- <venv python> -m pytest <file> -q -p no:cacheprovider --basetemp=.tmp/MS-2/bt_<name>`, two files at a time (`xargs -P2`), basetemp removed after each file. Counts are in the table. The two MS-2 files were rerun at the end of the sweep (rows `test_ms2_config_v2.py` and `test_ms2_manifest_v1.py`).

### Fixes (all in `tests/`; cause confirmed per file)

| File | Cause (missing now-required key or changed fact) | Fix |
| --- | --- | --- |
| `test_loader.py` (17 failed) | fixtures state a backend `image` without `max_images_per_call` (`:125,167,405`); `:444` expected manifest `schema_version: 2` refused, but 2 is `profile_manifest.v1` | added `max_images_per_call=1` (test-only, decision 179); the version test now uses 3, an unknown version, so the refusal is still checked |
| `test_model_config.py` (37), `test_model_composition.py` (50 then 1) | backend `image` dicts without the key; the writer-manifest case replaces the writer role by a drafter copy that has no `multi_source` | key added; the replaced writer keeps the example's `multi_source` block |
| `test_cli_config.py`, `test_cli_inputs.py`, `test_intake_config.py` | a request command now reads the `multi_source` prompt files at load, and the session `project_root` held no `prompts/` (the files passed before only when another test had installed them) | `conftest.project_root` copies the pipeline-owned prompts (same copy as `install_prompts`) |
| `test_pipeline.py`, `test_pipeline_s4_s5.py` | `validation_fixtures.environment` labelled the version-3 example as `schema_version: 2`, so the writer read validated a role with `multi_source` against the version-2 schema | label set to 3 |
| `test_validation_config.py` (legacy test) | built a "version 1" file that still carried the version-3 keys | the test strips the version-3 keys, as a real version-1 file lacks them |
| `test_real_contracts.py`, `test_profiles.py` | `_example` manifest is version 2 (Step 0 Q5, accepted); the test validated it against `profile_manifest.v0`, and the example drafter reply was checked against the v0 role schema | the test validates against `profile_manifest.v1` and asserts `schema_version == 2`; the registering test selects the v1 drafter schema id for its config |
| `test_multi_source_contracts.py` (2) | MS-1 test said no configuration names `planner_v2` or `brief_v1`; decision 179 and the accepted Step 0 make the examples name them under `multi_source.prompt_id` | design sentence: "multi_source.prompt_id" is the key that selects them. The test now parses each YAML under `configs/` and allows a draft prompt id only as the value of its own role's `multi_source.prompt_id`; `src` and every other key still may not name it (stricter in structure, not looser) |
| `test_probe_retry.py` (6, found in the rest-of-suite sweep) | fixture replaces the planner role by a copy of the observer role, dropping the required `multi_source` block | the replaced role keeps the example's block; 34 passed |
| `test_golden_retrieval.py` (3) | same missing `prompts/` | fixed by the `conftest` copy |
| `test_enhance_serve.py` (2) | failed once under load, passed on rerun (21 passed); no code or fixture change | none (flaky under load, recorded) |

### Findings

1. No loader defect and no changed meaning for an existing config or profile was found by the sweep. One-image bytes: `test_ms2_manifest_v1.py` pins them and passed.
2. **Behaviour note (not a defect):** a request command now reads `prompts/<id>.txt` of the two `multi_source` prompt ids at load (`config.py` `validate_source_limits`, phase 1). A deployment must ship the prompts under its project root; the per-request re-validation does not read them. The fixtures now provide them.
3. **Pre-mortem check (decision 169), per fixed file:** no assertion relaxed (the only expectation edits are `test_loader.py:444`, `test_real_contracts.py` schema choice and the `test_multi_source_contracts.py` rule above, each by design sentence); no key got a hidden default (every added key is a labelled test-only value or a strip of a version-3 key); no refusal message changed (`git diff HEAD -- src/` is the phase 1 and 2 diff only; the `match=` strings are untouched).
4. **`test_pipeline_s0_s6.py` (12 to 13 of 14 fail) and `test_pipeline.py::test_request_deadline_override_cancels_owned_work[3-success]` fail on this loaded machine, and fail identically at HEAD.** Cause (experiment): the trace row says `warnings: ['REQUEST_DEADLINE']`, `latency_ms` 7328 against `request_deadline_s` 5 (`test_pipeline_config.runtime()` states 5; the test's own parameter states 3). Baseline: a scratch copy of HEAD (`git show HEAD:<file>` over a copy of the tree, under `.tmp/MS-2/head`, removed) gives 13 failed, 1 passed for `test_pipeline_s0_s6.py` and the same 1 failure for `test_pipeline.py`, with the same errors. Probe: with the deadline raised (120 for `runtime()`, 60 for the parameter, both edits reverted, `git diff` empty) this worktree gives `test_pipeline_s0_s6.py` 14 passed and `test_pipeline.py` 18 passed. `cli_fixtures.py:71` already records the same cause for the CLI tests ("expired on a loaded machine"). Not fixed here: raising a shared test deadline is outside this task; the PM decides (see Noticed).
5. Timing check of the loader cost (idle machine): `_configuration_schema` takes 5 ms for a version-3 config against 3.8 ms for version 2 (`.tmp/MS-2/timing.py`, removed); no per-request slowdown from MS-2.

### Class sweep (fixtures that build a backend `image`, an `edit` block or a role by hand; file:line)

| Where | Needed the key? |
| --- | --- |
| `conftest.py:34,37`, `profile_fixtures.py:46,47` (phase 2) | yes, added |
| `test_loader.py:125,167,405` | yes, added |
| `test_model_config.py:65,270`, `test_model_composition.py:185,370` | yes, added |
| `test_model_composition.py:239` (replaced writer role) | yes, `multi_source` kept |
| `validation_fixtures.py:49` (`schema_version` label) | yes, 3 |
| `test_validation_config.py:284` (legacy file) | yes, strip |
| `test_model_config.py:154-155` (bare dicts to `ModelClientRun`, never loaded) | no: no loader reads them |
| `test_cloud1_anthropic.py:200`, `test_embed_server_config.py:341`, `test_model_client.py:131,193,230,341,406`, `test_model_client_image_side.py:49` (backend dicts passed to a client or server, not loaded) | no: those tests passed unchanged |
| `cli_fixtures.py:77,146`, `assembly_fixtures.py:120`, `intake_fixtures.py:34`, `test_assembly*.py`, `test_intake.py` (`edit` blocks that only set existing keys) | no: they start from the version-3 example |
| `test_cli_inputs.py:143,314,317`, `test_addendum_layout_config.py:211` (overlays at version 2) | no: a child below its base takes the base version (phase 1), they pass |

### Per-file results (one process per file; final run, with the first run when it differed)

Files that failed at first, still fail, or were not run:

| File | First run | Final run |
| --- | --- | --- |
| test_cli_config.py | 1 failed, 69 passed | 70 passed |
| test_cli_inputs.py | 5 failed, 65 passed, 1 skipped | 70 passed, 1 skipped |
| test_enhance_serve.py | 2 failed, 19 passed | 21 passed |
| test_golden_retrieval.py | 3 failed, 14 passed | 17 passed |
| test_intake_config.py | 15 failed, 16 passed | 31 passed |
| test_model_composition.py | 50 failed, 4 passed | 54 passed |
| test_model_config.py | 37 failed, 86 passed | 123 passed |
| test_multi_source_contracts.py | 2 failed, 95 passed | 97 passed |
| test_pipeline.py | 10 failed, 8 passed | 1 failed, 17 passed |
| test_pipeline_s0_s6.py | 13 failed, 1 passed | 12 failed, 2 passed |
| test_pipeline_s4_s5.py | 1 failed | 1 passed |
| test_probe_retry.py | 6 failed, 28 passed | 34 passed |
| test_profiles.py | 1 failed, 195 passed, 2 skipped | 196 passed, 2 skipped |
| test_real_contracts.py | 1 failed, 2 passed | 3 passed |
| test_validation_config.py | 1 failed, 168 passed | 169 passed |

`not_run` (the sweep queue stopped at the step budget; no result): res1_resume, response_lifecycle, retained_images, run_guide, run_settings, stage_flow, storage_anchored, storage_anchored_writes, storage_checkpoint_preservation, storage_checkpoint_view, storage_concurrency, storage_parquet_read, storage_properties, storage_references, storage_review_r1, storage_review_r2, storage_review_r3, storage_review_r4, storage_snapshots, storage_tables, storage_vanish_race, t017_fix2, trace_store, validation, validation_layout, validator_properties.

Green on the first and the final run: 128 files (names and per-file counts trimmed in fix round 1; they were listed in the phase 3 report).

### Open

1. The two deadline-bound files (Findings 4) need a decision on the shared 5 s deadline in `test_pipeline_config.runtime()`; they pass with it raised.
2. `ruff` is not installed in the project `.venv` (`not_run`).
3. Mutants: phase 2's 10 of 10 killed stand; phase 3 changed tests only, so no new mutants (`not_run` for test-only edits).
4. Files marked `not_run` in the table (if any) were still queued when the step budget ended.

### Noticed, not done

- The shared 5 s deadline makes seven-second-class tests flaky on a loaded machine (Findings 4); a labelled larger value in `runtime()` would remove it for every pipeline test.
- `prompts/README.md` still says `planner_v2` and `brief_v1` are "status: draft, not selected"; the example configs now name them under `multi_source`, and no stage runs them until MS-4 and MS-5. The test still reads that line; the README wording can change then.
- A `xargs`-style sweep of 85 files took about 2 hours here; `test_field_contracts.py` alone took 23 minutes.

**Changed files (phase 3, tests only):** `tests/conftest.py` (also phase 2), `tests/validation_fixtures.py`, `tests/test_loader.py`, `tests/test_model_config.py`, `tests/test_model_composition.py`, `tests/test_validation_config.py`, `tests/test_real_contracts.py`, `tests/test_profiles.py`, `tests/test_multi_source_contracts.py`, `tests/test_probe_retry.py`, this file. Earlier phases: see the lists in the progress log. Scratch under `.tmp/MS-2/` removed except `MULTI-SOURCE-step0.md`.


## Fix round 1 (phase 4, decision 164; decision 179 "Duyệt cả 5", decision 170 kept)

**Step 0 (Small: yes).** Unknowns: none blocking; the F2 structural decision was feasible, so the memoization fallback is not used. Plan: F1 one guard; F2 structural check plus a differential test; F3 to F6 small edits; run the listed files; 9 mutants. Finish check: the listed files green through `test_slot.py`, mutants killed, this section filled.

| Finding | Fix | Where (file:line, after the edit) | Test |
| --- | --- | --- | --- |
| F1 (P2) generate-only version-1 manifest refused at cap >= 2 | the version-1 branch now also needs `'edit' in manifest['supported_tasks']`, as the version-2 branch (`:559`) and `_require_attachable_renderer` (`:571`) do; docstring says it | `profiles.py:548-556` | `test_ms2_manifest_v1.py` `test_a_generate_only_version_1_manifest_still_loads_with_a_cap_of_two_or_more` (caps 2, 3, 7: loads, `supported_tasks == ('generate',)`), `test_an_edit_capable_version_1_manifest_is_refused_with_a_cap_of_two_or_more` (`['edit']` and `['generate','edit']`: `SourceLimitManifestMismatch`); the old fixed-message test stays |
| F2 (P2) overlap check rendered `cap` strings per request (0.49 s at cap 20000) | `overlapping_syntaxes` is now structural: for each pair of groups, each digit count of n and of m (at most the digits of the caps) and each offset, the two renderings are aligned position by position (literal/literal must be equal, literal over digit fixes the digit and must be 0-9, digit over digit ties); a walk over m's digits with two "still at the cap" flags decides the range limits, a leading zero is refused. No numeric ceiling, no new literal beyond `DIGITS` (protocol-bound: the characters of `str(n)`). Same return `(container, contained)`. `render_syntax` stays as the reference rendering (only tests call it now). Memoization fallback not needed | `config.py:2496-2620` (`render_syntax`, `_cells`, `_step`, `_can_occur`, `_occurs_within`, `overlapping_syntaxes`); caller `validate_renderer` `:2649` | `test_ms2_manifest_v1.py`: `test_the_structural_overlap_check_equals_the_rendering_oracle` (differential, 600 examples, counts to 300 plus boundary counts, affixes with digits, empty affixes, `none`, one or two groups; `None` iff the rendering oracle finds nothing, and a named pair is a real pair), `test_structural_overlap_cases_match_the_oracle` (15 fixed cases incl. leading-zero-free 1 inside 10, suffix starting with a digit, prefix ending with a digit, empty affixes, cross-group, none, and the post-constant case found by mutant 6), `test_the_overlap_check_does_not_render_and_costs_nothing_for_a_huge_cap` (`render_syntax` patched to raise; caps up to `sys.maxsize`). The earlier oracle in the property test is kept. Timing (`.tmp/MS-2/timing.py`, removed): `validate_renderer` 0.03 ms at cap 3, 0.28 ms at 20000, 1.6 ms at 10^9, 9.9 ms at `sys.maxsize` |
| F3 (P3) test proved nothing for `embed serve`, `eval import` | probe: both refuse identically at version 2 and version 3 for fixture reasons (`vlm.backends.embedder.engine.name: expected embed_server`; `evaluation.judge.score_min: unresolved sentinel design_required`). The test now asserts that exact refusal at version 2, no "migrate", and the same message at version 3; `kb status` and `profile register` assert they load | `tests/test_ms2_config_v2.py` `NO_KEY_COMMANDS`, `test_a_version_2_configuration_still_loads_for_commands_that_read_none_of_the_keys` | itself |
| F4 (P3) README said "not selected" | headings of `planner_v2` and `brief_v1` now read "status: draft, named by `multi_source.prompt_id` (used from MS-3 on; no request path uses it yet)"; the `planner_v2` body sentence "no code or configuration selects it today" now says the example configurations name it | `prompts/README.md:66,70,68` | `test_multi_source_contracts.py` `test_draft_prompts_exist_and_no_code_or_configuration_selects_them`: for the two keyed prompts it asserts the new heading text and no "not selected"; `manifest_drafter_v1` still asserts "status: draft, not selected"; the code and config scan is unchanged |
| F5 (P3) version 1 told "migrate to 2", then "migrate to 3" | on request commands the version-3 check runs first, so versions 1 and 2 both get "schema_version: migrate the configuration to version 3 (enhancer_config.v2): add ..."; the check is safe for a non-dict | `config.py:2808-2809` (order), `:2461-2463` (`isinstance` guard) | `test_ms2_config_v2.py` `test_a_request_config_below_version_3_gets_one_message_naming_version_3` (versions 1, 2 x 3 request commands, no "version 2" in the text); `test_validation_config.py:320` expected text moved to the version-3 message (the only `match=` that named the old text; `grep "migrate S5" tests src docs` shows none left); the version-2 message and its `match=` strings are unchanged |
| F6 (P3) `SOURCE_LIMIT_KEYS` unused | removed (the limit keys are matched by `_is_source_limit_key`, `config.py:1950`, which also covers `renderer.max_source_images`, so the tuple could not replace it) | `config.py:50` | grep: no other user in `src`, `tests`, `tools` |

**Class sweep (manifest version or renderer refusals; every one checks `supported_tasks` consistently).** `profiles.py:555` (version-1 pairing: now `'edit' in supported_tasks`), `:559` (version-2 reference recheck: `'edit' in supported_tasks`), `:571` (`AttachmentRendererMismatch`: `'edit' in supported_tasks`); `profiles.py:82` and `config.py:2968` (schema choice and renderer syntax check by version only; they refuse nothing about a task, correct); `pipeline.py:146` and `writer.py:93` read `renderer.accepts_reference_images` only for an edit task. No other branch. **Callers of `overlapping_syntaxes`:** `config.py:2649` (`validate_renderer`, reached from `load_profile` `:2968` and `profiles.py:560`); tests `test_ms2_manifest_v1.py` only. **Callers of `render_syntax`:** tests only (kept as the reference rendering; no `src` caller now).

**Mutants (scratch copy of `src`, `.tmp/MS-2/mutate.py`, removed; each run on the focused tests).** 9 of 9 killed: F1 guard dropped; F1 guard reads `generate`; F2 leading zero allowed (`j == 0` to `False`); F2 same-group equal digit counts compared (`>=` to `>`); F2 cap bound off by one (`>` to `>=`); F2 literal over a digit unchecked (`value not in DIGITS` dropped); F2 offsets truncated; F2 constants after the digits taken from m skipped (this one first survived: the differential test did not generate it; a random scan against the oracle found `{'s': (' {n}a', 14), 'r': (' {n}5a', 150)}`, now fixed cases, then killed); F5 order restored. `ruff`: `not_run` (not installed in the project `.venv`, as in phase 3).

**Commands and results** (`PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree>/src <venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -m pytest tests/<file> -q -p no:cacheprovider --basetemp=.tmp/MS-2/bt_<file>`, one file per process): `test_ms2_config_v2.py` 115 passed; `test_ms2_manifest_v1.py` 97 passed (final, after the two regression cases; 95 before); `test_loader.py` 135 passed; `test_profiles.py` 196 passed, 2 skipped; `test_multi_source_contracts.py` 97 passed; `test_cli_config.py` 70 passed; `test_validation_config.py` 169 passed; also `test_intake_config.py` 31 passed and `test_cli_inputs.py` 70 passed, 1 skipped (they load request configs). The F2 differential test runs 600 examples inside `test_ms2_manifest_v1.py`.

**Pre-mortem (decision 169), each tested.** Leading zeros (the `_step` leading rule; mutant 2 killed); suffix starting with a digit and prefix ending with a digit (`'{n}0'`, `'a1{n}'` cases; random affixes carry digits); empty prefix or suffix (`'{n}'` cases and random affixes); cross-group overlap (the differential test draws two groups; `'<{n}>'` vs `'{n}'`); F1 guard reads the wrong field (mutant 1 killed; `['edit']`-only manifest refused); message change breaking a `match=` (`grep` plus the seven files and the two extra files above, all green; the one old-text `match=` was updated, and no refusal text other than the version-1 request-command one changed).

**Configuration audit (rows touched).** `edit.max_source_images` pairing: unchanged key; the refusal now applies only to edit-capable version-1 manifests (override test: generate-only loads at caps 2, 3, 7; refusal test: edit-capable refused). `DIGITS` literal: protocol-bound (the characters `str(n)` produces). No new setting, no ceiling, no default; the deleted `SOURCE_LIMIT_KEYS` row in the earlier audit table no longer applies (the matcher `_is_source_limit_key` stays, classified safety-bound). Product rules R-001, R-004, R-009, R-026: no code path touched reads the knowledge base, the system prompt or user text; no weakening.

**Noticed, not done.** `tests/test_zz_probe.py` is an empty scratch file I could not delete (the shell guard forbids removing files under `tests/`); it is untracked and holds one comment line; the PM deletes it before staging. The shared 5 s deadline item stays with the PM. `overlapping_syntaxes` assumes exactly one `{n}` per syntax (`syntax_error` guarantees it at both call sites); a hand call with none or two `{n}` is outside its contract.

**Changed files (phase 4):** `src/prompt_enhancer/config.py`, `src/prompt_enhancer/profiles.py`, `prompts/README.md`, `tests/test_ms2_manifest_v1.py`, `tests/test_ms2_config_v2.py`, `tests/test_multi_source_contracts.py`, `tests/test_validation_config.py`, this file (per-file table of phase 3 trimmed to failures and counts, 43.8 KB to 38.8 KB; with this section 48.7 KB, over the 25 KB target: the approved Step 0 and the earlier phases are history owned by the PM, who can archive them). Scratch under `.tmp/MS-2/` removed except `MULTI-SOURCE-step0.md`.
