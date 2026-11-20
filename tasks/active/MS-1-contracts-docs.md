# MS-1: multi-source contracts (v1 schemas) and prompt drafts

Kind: docs
R-IDs: R-001, R-002, R-004, R-009, R-011, R-012, R-015, R-024, R-026, R-027
Task file: tasks/active/MS-1-contracts-docs.md
Approach note: `.tmp/MS-1/MULTI-SOURCE-step0.md` (decisions 176, 179, 182, 196)
Trivial: no. Small: no. Status: phase 2 done (version renames, see "Phase 2" below), waiting for review. Step 0 approved by the PM with answers 1 to 5 as proposed (see "Approved answers"). Names in Step 0, Steps and the phase 1 report that say `intake_result.v1`, `response_envelope.v1` or `golden_case.v1` for the multi-source contracts are the phase 1 history; phase 2 renames them to v2.

## Approved answers (phase 1)

1. The multi-source plan result is `plan_result.v2` (v1 plus `intent_ir.v1`), because `plan_result.v1` exists from T017-FIX. The docs correct the design's version table and say why (`contracts/README.md`, "Why `plan_result.v2`").
2. (Replaced in phase 2: the multi-source intake result is `intake_result.v2` and references `response_envelope.v1` from T017-FIX3, so "MS-5 widens it in place" is dropped.) Phase 1 text: `intake_result.v1.envelope` references `response_envelope.v0` for now; MS-5 widens it in place, allowed only because `intake_result` is never stored.
3. The eval `.v1` rows are skipped (see `Noticed, not done`).
4. Schemas enforce 1-based integers, non-empty lists and uniqueness. The range check (each index at most n) and the n of 2 or more rule live in code (MS-3, MS-4); the docs and prompt drafts state them. Fixtures mark the schema-valid, code-refused cases.
5. `manifest_drafter_v1` is written and stays unselected.

## Change of plan (to be written into the docs where the design's order is described)

`response_envelope`, `golden_case`, `profile_manifest.v1` and `enhancer_config.v2` leave MS-1. T017-FIX3 owns the envelope, golden and profile-manifest files and CLOUD-1 owns the cloud and model-client code, so these four move to MS-2 (manifest, config) and later (envelope, golden), which start from the integrated fixes. MS-1 keeps the other rows of the design's "Contract versions" table. Phase 2 correction: the envelope is `response_envelope.v2` (MS-5) and the golden case is `golden_case.v2` (MS-6), see "Phase 2".

## Phase 2: version collision with T017-FIX3 (PM message "MS-1 phase 2")

T017-FIX3 lands before the multi-source tasks and owns `intake_result.v1` (refusal envelope under `response_envelope.v1`), `response_envelope.v1` (closed `retrieval.error` code, state `not_reached`, decisions 198 and 200) and `golden_case.v1` (golden retrieval switch). Read-only look at its worktree: `intake_result.v1` differs from `intake_result.v0` only in the envelope reference, so the multi-source intake result stays derived from v0 and takes the next number. Table after the correction (docs: `contracts/README.md` "Why `intake_result.v2`, `response_envelope.v2` and `golden_case.v2`"):

| Contract | T017-FIX3 / today | Multi-source | Written by |
| --- | --- | --- | --- |
| intake result | `intake_result.v1` | `intake_result.v2` (references `response_envelope.v1`; "widen in place" dropped) | MS-1 |
| response envelope | `response_envelope.v1` | `response_envelope.v2` | MS-5 |
| golden case | `golden_case.v1` | `golden_case.v2` | MS-6 (was MS-5) |
| plan result | `plan_result.v1` | `plan_result.v2` (unchanged from phase 1) | MS-1 |

Done: renamed the schema file, its `$id`, the fixture file and the test; the `$ref` points at the id `response_envelope.v1`, which this worktree does not hold, so the fixture tests of `intake_result.v2` (15 cases) skip with the reason "waiting for T017-FIX3 ..." and the PM re-runs them after the merge. A new test asserts the reference by id. Corrected the version table, the stage docs S0 and S6 and the section status line (golden cases move to MS-6).

## Step 0

### 1. Unknowns and failure modes (evidence read: source-verified unless marked)

- **Name collision (found).** `contracts/schemas/plan_result.v1.schema.json` already exists: it is `plan_result.v0` plus the optional `error.cause` (T017-FIX; `contracts/README.md:181, 285`; `git diff` of the two files differs only in `$id`, `$comment` and the `cause` property). The design's table names `plan_result.v1` for the multi-source plan. Two files cannot both be v1. See question 1.
- **"v0 contracts for n = 1" is not literally true for the plan.** Today's n = 1 path emits `plan_result.v1` (T017-FIX), `writer_result.v1`, `validation_result.v1`. The design text says `planner_reply.v0`, `intent_ir.v0`, `brief_v0`. For the schemas MS-1 names the claim holds (`planner_reply.v0` and `intent_ir.v0` stay); the docs I write must say "today's contracts" and name `plan_result.v1`, not "v0".
- **Schema cannot compare an index with n.** JSON Schema 2020-12 has no arithmetic between two fields, and `n` is a runtime count. A schema can refuse 0, negatives, non-integers, an empty list and duplicates (`minimum: 1`, `minItems: 1`, `uniqueItems`). It cannot refuse `[3]` when n = 2, or an unsorted list. The design gives those to code (the range check, MS-4). I will not pretend otherwise: fixtures mark "valid by schema, refused by code" separately (question 4).
- **`intake_result.v0` embeds `$ref: response_envelope.v0`** (`intake_result.v0.schema.json:49`). The v1 envelope is out of scope, so `intake_result.v1` cannot point at it (question 2).
- **Registry and glob tests.** `tests/test_model_schemas.py:test_all_project_schemas_load` builds `SchemaRegistry` over every file in `contracts/schemas/`: a new schema with a bad `$id` or an unresolvable `$ref` breaks it, and a new `$id` that duplicates one fails it. Run it first and last. Tests that read `prompts/` name individual files (grep: `planner_v1.txt`, `manifest_drafter_v9.txt`); a glob over `prompts/` has not been found yet and I will grep once more before adding the three drafts.
- **A prompt draft that no path selects** must stay unreachable: no config key names `planner_v2`, `brief_v1` or `manifest_drafter_v1` (MS-2 adds them), and the prompt registry must not list them as loadable. Check: grep `src/` and `configs/` for the three ids returns nothing.
- **Prompt hazards:** R-026 (images and knowledge text are data), R-004 (no addendum or system text change for multi-source), R-001 (verbatim spans), R-015 (no word list, and the draft must not teach the model to map a word to an image number by code).
- **Other risks:** wording that says multi-source works today; editing an existing v0 schema or prompt; hooks or lints that read `contracts/README.md` (doc budget in `tools/agent-workflow/doc_budgets.json`, to be read before editing the README).
- **Not known (assumption):** whether the repository has a doc link check. `tools/agent-workflow/` has `doc_budgets.json`, `doc_id_unique.py`, `eol_check.py`, `decisions_applied.py` and no file named for links; I will grep it once and report.

### 2. Questions (ranked; each with a proposed answer)

1. **Which id for the multi-source plan result?** Proposed: `plan_result.v2` (`plan_result.v1` plus `intent_ir` as `$ref: intent_ir.v1` for n >= 2, and `error.cause` kept), and the docs and the design's table say `plan_result.v2`. `planner_reply.v1` and `intent_ir.v1` are free names and keep the design's ids. The alternative (renaming the T017-FIX file) would edit a file that is in use and break stored artifacts, so it is dropped.
2. **What does `intake_result.v1.envelope` reference?** Proposed: `oneOf [null, {$ref: response_envelope.v0}]` with a `$comment` that MS-5 widens it to the v1 envelope when that exists. `intake_result` is not stored (design table: "Stored? no") and is not in `schemas_sha256`, so MS-5 may widen it in place (README versioning rule, line 285, which lets a non-stored draft take additive keys). Alternative: leave `envelope` out of v1 until MS-5; that breaks the refusal path of the schema.
3. **Eval rows (`eval_scenario.v1`, `eval_judge_pack.v1`)?** Proposed: not written. The design gives only deltas (`source_images: [{image_id, folder_id}]`, `source_indexes` on key targets, `base_source_index`), not a full shape, and T-003 still owns `eval_scenario`'s `draft_*` fields (design "Noticed"). They go to `Noticed, not done` for MS-6. The brief allows this ("only if the design gives their shape").
4. **How do the schemas express "index in range" and "n of 2 or more"?** Proposed: schemas enforce what a schema can (1-based integers, `minItems: 1`, `uniqueItems`, nullable `base_source_index` on `edit` kinds with the `subject_recompose` rule written as an `if/then`; `planner_reply.v1` and `intent_ir.v1` are the n >= 2 revisions and require `source_indexes` on every tagged item). The range (each index <= n) and the ascending order stay with code in MS-4, written in the contract docs as "checked by code, not by the schema", and the fixtures name a "schema-valid, code-refused" instance. A `minItems: 2` rule is placed on the planner data's `source_images` list only if the data shape is a contract file (it is not; it is a prompt data block), so the n >= 2 rule lives in the docs and in `brief_v1`/`planner_v2` text, not in a schema.
5. **How far can `manifest_drafter_v1` go before `profile_manifest.v1` exists?** Proposed: write the draft prompt so it asks for the three renderer values named in the design (`max_source_images`, `source_image_syntax`, `reference_image_syntax`, and the `none`/`1` rule from the design), and mark its reply shape "pinned by MS-2 with `profile_manifest.v1`". It stays unselected. I will read `manifest_drafter_v0.txt` and its reply contract first and keep every other byte of its rules.

### 3. Plan (what exists, what is missing, steps, finish checks)

Exists: v0 schemas and `planner_v1`, `brief_v0`, `manifest_drafter_v0`; `plan_result.v1` (T017-FIX); `contracts/README.md:59` already says several sources in `request.v1` (decision 182). Missing: everything below.

1. Read `intent_ir.v0`, `planner_reply.v0` in full, `manifest_drafter_v0.txt`, the four stage docs, `contracts/README.md` sections on S0, S2, S4, S6 and versioning, `doc_budgets.json`. Run the existing contract tests as a baseline.
2. Schemas (`request.v1`, `intake_result.v1`, `planner_reply.v1`, `intent_ir.v1`, `plan_result.v2`): each a full copy of its v0 plus the design's changes, written by a throw-away script under `.tmp/MS-1/` (removed at the end) so the v0 bytes are unaltered and the copies do not drift by hand.
3. Fixtures `tests/fixtures/contracts/multi_source/<schema>.{valid,invalid}.json` (and a `schema-valid, code-refused` set) and `tests/test_multi_source_contracts.py`: `check_schema` per new schema, valid passes, invalid fails, a guard that every `*.v0` schema and `plan_result.v1` is byte-unchanged (sha256 list taken from `HEAD`), and a guard that no `src/` or `configs/` file names the three draft prompts.
4. Prompt drafts (`planner_v2.txt`, `brief_v1.txt`, `manifest_drafter_v1.txt`), each with the draft marker in `prompts/README.md` (the text files stay loadable verbatim, so the marker goes in the README entry, not into the prompt bytes; a header line inside a prompt would be sent to a model).
5. Docs: `contracts/README.md` (new versions, dispatch on version, worked examples as contract examples, the change of plan, `plan_result.v2`), S0, S2, S4, S6 "designed, not yet implemented (MS-2 to MS-7)", `docs/architecture.md` only where the design order is described, `prompts/README.md` entries. The class sweep result is below.
6. Verify: test runs via `test_slot.py`, `git diff --stat` (only new files plus the listed doc edits), pre-mortem table, Completion report.

Finish checks mapped to the done check: new contract test green; `tests/test_field_contracts.py`, `tests/test_model_schemas.py` and any registry or glob test green; `git diff --name-only` shows no existing `*.schema.json` or `prompts/*.txt`; invalid fixtures fail their schemas (shown in the test output); the Completion report is in this file.

### Class sweep: doc lines that still say `edit` takes exactly one image (grep over docs, contracts, prompts, README, AGENTS, REQUIREMENTS)

Proposed per line in Step 0 (a table of 10 rows); the result is under "Class sweep: doc lines that said `edit` takes exactly one image" below, which is the single owner of the decisions.

## Steps

1. Read `planner_reply.v0`, `intent_ir.v0`, `intake_result.v0`, `request.v0`, `plan_result.v1`, `manifest_drafter_v0`, `planner_v1`, `brief_v0`, the S0, S2, S4, S6 docs and the schema registry; found that the model-facing schema is sent to the server as `response_format` (`model_mapping.py:140`), so `planner_reply.v1` avoids `if`/`then` and `$ref`.
2. Wrote the five schemas by a throw-away script from the unchanged v0 files (removed afterwards): `planner_reply.v1` (model-facing, no `if`/`then`), `intent_ir.v1` and `plan_result.v2` (code-built, with the linkage rules), `request.v1`, `intake_result.v1` (adds `request_version`, `source_images`, keeps the envelope reference).
3. Wrote five fixture files (`tests/fixtures/contracts/multi_source/<schema>.json`: base, valid, invalid, code-refused cases) and `tests/test_multi_source_contracts.py` (96 cases plus the byte guard for nine existing files and the "no code selects the drafts" guard).
4. Wrote `planner_v2.txt`, `brief_v1.txt`, `manifest_drafter_v1.txt` and their three "draft, not selected" entries in `prompts/README.md`.
5. Docs: new section "Multi-source contracts (designed, not yet implemented)" in `contracts/README.md` (versions table with the `plan_result.v2` correction, reading by version, schema-versus-code table, the `intake_result.v1.envelope` note, the worked examples) plus pointers at four lines of it, and one "designed, not yet implemented" paragraph in each of S0, S2, S4, S6. `docs/architecture.md:7` already says several sources, "built in MS-1 to MS-7; today's code accepts one", so it is unchanged.
6. Ran the tests, a link and anchor check of the six edited docs, `eol_check`, a mutant-style probe of the schemas, the diff check, and cleaned `.tmp/MS-1/` (only the approach note stays).

## Configuration impact and audit

No runtime edit: no code, no configuration key, no profile value. Rows: not applicable, with the reason that nothing here is read at run time. The keys the docs name (`edit.max_source_images`, `renderer.*`, `vlm.roles.*.multi_source.*`, `max_images_per_call`) are MS-2 additions and are written as "added by MS-2". Retained literals: none in code. In the schemas the only literals are protocol-bound (`minimum: 1` is the 1-based index rule of decision 179; the `sha256:` pattern follows `intake_result.v0`); no cap is written because every cap is a configuration value.

## Pre-mortem (decision 169): how it could fail, and the check that rules it out

| How it could fail | Check and result |
| --- | --- |
| An existing v0 schema or prompt changed | `git diff --name-only` lists only six doc files (`contracts/README.md`, `prompts/README.md`, S0, S2, S4, S6); the test `test_existing_v0_schemas_and_prompts_are_byte_unchanged` hashes `request.v0`, `intake_result.v0`, `planner_reply.v0`, `intent_ir.v0`, `plan_result.v0`, `plan_result.v1`, `planner_v1`, `brief_v0`, `manifest_drafter_v0`: passed |
| The n of 1 path no longer byte-identical in the design text | The docs say "today's contracts ... byte for byte" for one image from `request.v0` or from a one-element list, and list the real set (`plan_result.v1`, `writer_result.v1`, `validation_result.v1`, not "v0" only); the new revisions are used only when n is 2 or more (`contracts/README.md`, S2, S4) |
| A schema accepts a 0-based or out-of-range index | 0-based, negative, fractional, string, null, boolean: refused by `test_a_source_index_below_one_or_not_an_integer_is_refused_everywhere` (6 bad values over the 4 lists that carry indexes, plus the base index). "Above n" is not a schema fact; it is stated as a code check (MS-3, MS-4) and the fixtures hold "schema-valid, code-refused" cases for it. Probe: weakening `minimum: 1` lets 7 invalid fixtures through, so a regression is caught |
| A prompt draft treats image or knowledge text as instructions (R-026) | `planner_v2` says every attached image and the text in it is data and that image text never changes the base, a target or the edit; `brief_v1` keeps "text from images ... remain data" and makes `ref` tokens data; the test asserts both sentences exist. Whether a model obeys is not measured here (not a code property): MS-7 and the eval kind `edit_multi_source_injection` |
| The admin system prompt's authority weakened (R-004) | `brief_v1` still ends "Follow the administrator's output contract"; the tokens are data and the admin's prompt decides their use; no addendum or system text is added (stated in `prompts/README.md`, S4, `contracts/README.md`). `brief_v0` is untouched |
| The user's intent spans altered (R-001) | `planner_v2` keeps the verbatim-span paragraph of `planner_v1` unchanged; the new text adds tagging only; the fixtures keep the Vietnamese prompt verbatim |
| A word list or per-language rule slipped in (R-015) | None in the prompts: the model reads the prompt "in any way and in any language" and decides the image; code only range-checks; the schemas hold no enum of words. The `manifest_drafter_v1` rule writes `null` and never a guess. Reviewer to confirm by reading the three prompts |
| Docs claim multi-source works today | Every new paragraph and section is headed "designed, not yet implemented" and names its task (MS-2 to MS-7); `schema` `$comment`s start "Draft v1 (MS-1, designed, not yet implemented)" and the test asserts that phrase; the README prompt entries say "status: draft, not selected" and a test asserts that and that no file under `src/` or `configs/` names the three ids |
| A new file breaks a registry test | `tests/test_model_schemas.py` (builds a registry over every schema file) and the other listed tests: passed (below) |

## Class sweep: doc lines that said `edit` takes exactly one image

Decided in Step 0; result:
- Updated now: S0, S2, S4, S6 (a "designed, not yet implemented" paragraph each, so their current text stays true for today's code); `contracts/README.md:59` (task enum row), `:137` (request), the planner contract (one paragraph before "Role image sources"), and the versioning list.
- Left true as written, for now: `S0-intake.md:5, 10, 11, 35`, `S2-planner.md:5, 20, 29`, `S4-writer.md:5, 13`, `S6-envelope.md:8` (describe today's code; each stage doc now points at its designed change). `docs/architecture.md:7, 53` already say one or more. `README.md:3`, `AGENTS.md:67`, `tasks/REQUIREMENTS.md:10, 15` already say one or more (decision 182). `K2-observe.md`, `K3-cards.md`, `S3-knowledge.md` say one image per call about knowledge images, not `edit`: no change.
- Later (listed under `Noticed, not done`): `docs/stages/G-evaluation.md:5` and `docs/run-guide.md:186, 219` (MS-6, run guide), `docs/stages/P-profiles.md:27`, `configs/profiles/_example/README.md:9` (MS-2), `contracts/vlm-request.md:35` and `contracts/README.md:192-193` role image source `edit_source_only` (MS-4), `docs/architecture.md:27` table cell "sees the source image" (it is true for n = 1; revisit with MS-5).

## Noticed, not done

- For MS-6: `eval_scenario.v1` and `eval_judge_pack.v1` (the design gives deltas only: `source_images: [{image_id, folder_id}]`, `source_indexes` on key targets and preserve entries, `base_source_index`; T-003 still owns the `draft_*` fields). Also the new kinds `edit_multi_source` and `edit_multi_source_injection`.
- Moved out of MS-1 (phase 2 names): `profile_manifest.v1` and `enhancer_config.v2` (MS-2), `response_envelope.v2` (MS-5), `golden_case.v2` (MS-6). `manifest_drafter_v1` waits for `profile_manifest.v1`: its reply shape (a `null` value for a renderer field the prompt does not state) is MS-2's to pin or change.
- Not in scope: the source photos for eval scenarios (the user chooses them); a `brief_v1` judged by an eval; every runtime change; `docs/architecture.md` (line 7 already says several sources and "built in MS-1 to MS-7").
- The design note `MULTI-SOURCE-step0.md` is not in the repository tree of this worktree (only under `.tmp/MS-1/`), so the docs cite decisions and this task file and link nowhere to it; its header names `tasks/active/MULTI-SOURCE-step0.md`: the PM may place it there.
- Class sweep "later" lines: `docs/stages/G-evaluation.md:5`, `docs/run-guide.md:186, 219` (MS-6 and the run guide), `docs/stages/P-profiles.md:27`, `configs/profiles/_example/README.md:9` (MS-2), `contracts/vlm-request.md:35` and `contracts/README.md:192-193` (`edit_source_only`, MS-4), `docs/architecture.md:27` (revisit with MS-5).
- Probe: the model-facing `planner_reply.v1` uses `anyOf` with `minimum`, `minItems` and `uniqueItems` as `planner_reply.v0` does (v0 has `minimum`, not `uniqueItems`); whether the local grammar backend accepts `uniqueItems` is not verified (the design's risk row, MS-7 smoke). Client-side validation refuses a repeated index either way.
- `contracts/README.md` line 177-180 says the same verification gap for `maxItems`; unchanged.
- `contracts/README.md:257` still says configuration `schema_version` must be `1` (design "Noticed"); `docs/stages/S4-writer.md:31` stale degraded mode (KB-EXTERNAL).

## Completion report

- **Work completed:** five draft schemas (`request.v1`, `intake_result.v1`, `planner_reply.v1`, `intent_ir.v1`, `plan_result.v2`) under `contracts/schemas/`; five fixture files and `tests/test_multi_source_contracts.py`; three prompt drafts (`planner_v2.txt`, `brief_v1.txt`, `manifest_drafter_v1.txt`) with README entries "draft, not selected"; the multi-source section of `contracts/README.md` with the corrected version table and the worked examples; one "designed, not yet implemented" paragraph each in S0, S2, S4, S6; the change of plan for the four rows that moved to MS-2 and MS-5. No runtime code, no configuration key, no existing schema or prompt changed.
- **Result and evidence:** the done-check output, pasted below (commands run from the worktree root through `test_slot.py`, interpreter `.venv/Scripts/python.exe`, `PYTHONPATH` set to this worktree's `src`).

```
$ test_slot.py -- python -m pytest tests/test_multi_source_contracts.py -q -p no:cacheprovider
........................................................................ [ 75%]
........................                                                 [100%]
96 passed in 2.66s

$ test_slot.py -- python -X faulthandler -m pytest tests/test_multi_source_contracts.py tests/test_field_contracts.py tests/test_model_schemas.py tests/test_planner.py tests/test_profiles.py tests/test_intake.py tests/test_config_links.py -q -p no:cacheprovider --basetemp=.tmp/MS-1/basetemp
993 passed, 36 skipped in 287.74s (0:04:47)

$ git diff --name-only      (tracked files changed: docs only)
contracts/README.md
docs/stages/S0-intake.md
docs/stages/S2-planner.md
docs/stages/S4-writer.md
docs/stages/S6-envelope.md
prompts/README.md

$ git status --short | grep '^??'      (new files)
contracts/schemas/{intake_result.v1,intent_ir.v1,plan_result.v2,planner_reply.v1,request.v1}.schema.json
prompts/{brief_v1,manifest_drafter_v1,planner_v2}.txt
tasks/active/MS-1-contracts-docs.md
tests/fixtures/contracts/   tests/test_multi_source_contracts.py

$ eol_check.py <the six edited docs>
eol_check: clean (D:\Project_github\prompt-enhancer-pipeline\.tmp\worktrees\MS-1)

$ link and anchor check of the six edited docs (throw-away script, removed)
bad links: 0

$ probe: weaken a scratch copy of the schemas, count invalid fixtures that then pass (all caught)
M1 drop minimum 1: 7 accepted   M2 drop uniqueItems: 2   M3 drop minItems: 4   M4 source_indexes not required: 4
M5 drop all if/then/else: 8   M6 drop additionalProperties: 2   M7 edit_kind nullable: 1
```

  The 36 skips are the existing platform and optional-dependency skips of those files (not counted against this task; their reasons were not re-read). The invalid fixtures failing their schemas is shown by `test_invalid_examples_fail` (every invalid case changes a valid base, so it fails for its own edit) and by the probe above.
- **Important changes and reasons:** `plan_result.v2`, not `v1` as the design table said, because `plan_result.v1` already exists (T017-FIX); the docs correct the table and say why. `planner_reply.v1` is model-facing and goes to the server as `response_format`, so it has no `if`/`then`, no `$ref`; the linkage rules (null base only for `subject_recompose`; `source_indexes` exactly when the source is `source_image`) are in `intent_ir.v1`, which code builds. `intake_result.v1` adds `request_version` (from the design's "recording `request_version`") so the envelope version can follow the request. `request.v1` writes no `maxItems`: every cap is a configuration value (R-015, AGENTS.md section 5).
- **Problems, risks and remaining work:** the range check (index at most n), the ascending order and the n of 2 or more rule are code checks (MS-3, MS-4); the schemas cannot express them and the docs say so. Prompt drafts are not measured: how a model follows `planner_v2` and `brief_v1` is for MS-7 and the eval (`edit_multi_source_injection`). `manifest_drafter_v1` writes `null` for a renderer value the prompt does not state; MS-2 pins that with `profile_manifest.v1`. Grammar acceptance of `uniqueItems` is unverified. `ruff format --check` is `not_run`: `ruff` is not installed in the project `.venv` (no install, AGENTS.md section 4); the test file follows the surrounding style by hand. Mutants: not applicable to docs; the probe above replaces them. The design note is not in the tracked tree (see `Noticed, not done`).

### Research evidence

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| Does `plan_result.v1` already exist, and what is in it? | `contracts/schemas/plan_result.v1.schema.json:1-8` at base commit `28f0557` | raw | It is `plan_result.v0` plus `error.cause` (T017-FIX); the one-source path emits it (`contracts/README.md:181`) | source-verified |
| Is the model-facing schema sent to the server? | `src/prompt_enhancer/model_mapping.py:140-141` | raw | `output['format'] == 'json_schema'` becomes `response_format` with the schema, so `planner_reply.v1` avoids `if`/`then` and `$ref` | source-verified |
| Do schemas resolve `$ref` by id in a registry, and does a bad `$id` fail? | `src/prompt_enhancer/model_schemas.py:81-97` | raw | `$id` must equal the file name; `$ref` resolves through the registry (`intent_ir.v1` references `constraint_ledger.v0`, `plan_result.v2` references `intent_ir.v1`) | source-verified |
| Can a JSON Schema relate an index to n? | JSON Schema 2020-12 keywords as used in the repo (no arithmetic keyword; `check_schema` accepts the files) | summary | No: bounds and uniqueness only; the range check is code | assumption |
| Is `contentEncoding: base64` enforced? | `jsonschema` behaviour observed: the fixture "text that is not base64" passes `request.v1` | experiment | It is an annotation; S0 decodes and refuses (MS-3) | experiment |
| Which design points did I rely on? | `.tmp/MS-1/MULTI-SOURCE-step0.md` Q1 to Q5, "Change plan", "Contract versions", "Worked examples"; `tasks/DECISIONS_INDEX.md` rows 176, 179, 182, 196 | raw | Used as written, except the `plan_result` name | source-verified |

### Configuration audit

No runtime edit and no configuration key: nothing to audit. The retained literals in the new files are protocol-bound (the 1-based index rule `minimum: 1`, the `sha256:` image id pattern copied from `intake_result.v0`); no cap, limit or default is written.

## Completion report (phase 2)

- **Work completed:** the multi-source intake result is now `intake_result.v2` (schema file, `$id`, fixture file `tests/fixtures/contracts/multi_source/intake_result.v2.json`, the test list); its `envelope` references `response_envelope.v1` by id and the "widen in place" note is gone; the version table in `contracts/README.md`, the S0 and S6 paragraphs and the section status line are corrected (`response_envelope.v2` is MS-5, `golden_case.v2` is MS-6) with the reason given under "Why `intake_result.v2`, `response_envelope.v2` and `golden_case.v2`"; `plan_result.v2` is unchanged. The 15 fixture cases of `intake_result.v2` skip while `response_envelope.v1.schema.json` is absent, with a stated reason; a new test checks the reference by id. Nothing outside my files was edited (T017-FIX3's worktree was only read).
- **Result and evidence:** commands from the MS-1 worktree root through `test_slot.py`, `PYTHONPATH` set to its `src`.

```
$ test_slot.py -- python -X faulthandler -m pytest tests/test_multi_source_contracts.py tests/test_model_schemas.py tests/test_field_contracts.py -q -rs -p no:cacheprovider --basetemp=.tmp/MS-1/basetemp
SKIPPED [15] tests\test_multi_source_contracts.py:75: waiting for T017-FIX3: response_envelope.v1 (referenced by intake_result.v2) is not in this tree; the PM re-runs this test after the merge
SKIPPED [33] tests\test_field_contracts.py:177: No alternative found: schema may constrain this field to a protocol constant
SKIPPED [1] tests\test_field_contracts.py:247: No supported alternative found for manifest field
636 passed, 49 skipped in 227.56s (0:03:47)

$ scratch probe: the intake_result.v2 fixtures against a registry that adds a read-only copy of T017-FIX3's draft response_envelope.v1 (scratch copy removed afterwards)
valid+code_refused accepted: 7; invalid refused: 8 of 8

$ link and anchor check of the six edited docs; eol_check on the three docs edited in this phase
bad links: 0
eol_check: clean (D:\Project_github\prompt-enhancer-pipeline\.tmp\worktrees\MS-1)

$ git diff --name-only      (tracked files changed, unchanged set: docs only)
contracts/README.md
docs/stages/S0-intake.md
docs/stages/S2-planner.md
docs/stages/S4-writer.md
docs/stages/S6-envelope.md
prompts/README.md
```

  The 15 skips of this task are the `intake_result.v2` cases (3 valid, 8 invalid, 4 code-refused); the other 34 skips are the existing ones of `test_field_contracts.py`. The skipped cases are not evidence yet: the scratch probe shows the fixtures behave as written against T017-FIX3's envelope draft, and the PM's re-run after the merge is the real check. If T017-FIX3 changes `response_envelope.v1` before it lands, the re-run decides.
- **Important changes and reasons:** the three version numbers follow ownership: T017-FIX3 holds v1 of the intake result, the envelope and the golden case, so the multi-source successors are v2, which avoids two files for one id and keeps T017-FIX3's files byte-untouched. `intake_result.v2` is derived from `intake_result.v0` because T017-FIX3's `intake_result.v1` differs from it only in the envelope reference (read in its worktree, not copied).
- **Problems, risks and remaining work:** after the merge the PM re-runs `tests/test_multi_source_contracts.py` (the 15 skips must turn into passes). If T017-FIX3 later changes `intake_result.v1` beyond the envelope reference, `intake_result.v2` must take the change too. Which envelope a refused `request.v1` carries (v1 or v2) is left to MS-5. `ruff format --check` is `not_run` (ruff is not installed in the project `.venv`). The phase 1 report above is kept as written and uses the old v1 names for these two contracts.

### Research evidence

| Question | Source (URL or file plus version, commit or tag) | Read raw or summary | Finding | Class: documented / source-verified / experiment / assumption |
|---|---|---|---|---|
| What did T017-FIX3 change in the intake result and the envelope? | `.tmp/worktrees/T017-FIX3/contracts/schemas/intake_result.v1.schema.json:3-5, 50` and `response_envelope.v1.schema.json:5` (untracked drafts, base commit `a8b2f27`), read only | raw (diff against the v0 files) | `intake_result.v1` is v0 with the envelope reference changed to `response_envelope.v1`; the envelope v1 adds `retrieval.error` and the state `not_reached` (decisions 198, 200) | source-verified |
| Does a `$ref` to a missing id fail at registry build or at validation? | `src/prompt_enhancer/model_schemas.py:81-97`; this run: `test_all_project_schemas_load` passes without the file | raw, experiment | The registry is built without resolving references; validating an instance of `intake_result.v2` needs the target, so those cases skip | source-verified |

### Configuration audit

No runtime edit and no configuration key: nothing to audit.
