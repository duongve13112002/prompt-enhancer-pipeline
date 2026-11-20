# Data contracts

The single specification of identities, tables, enums, artifacts and the profile contract. Nothing is implemented and nothing is measured: no schema code exists, and every schema file is a draft v0. A change here is a contract change: update the stage documents in [docs/stages/](../docs/stages/README.md) and the configuration examples in `configs/` in the same change. How runs, resume and snapshots work: [docs/execution.md](../docs/execution.md).

## Terms

- **Configuration profile** is a YAML file under `configs/` (for example `configs/enhancer.example.yaml`). **Admin profile** is a system prompt plus its manifest under `configs/profiles/<profile_id>/` ([P-profiles](../docs/stages/P-profiles.md)). The words are never interchangeable.
- **Role** has three uses: the **role of a concept in a request** (`subject_identity`, `worn_garment`, and so on, assigned by the planner); the **image role** (`source_image`, `user_reference`, `knowledge_reference`); and the **VLM role** (a key under `vlm.roles`, such as `planner` or `writer`).
- **`family`** is a concept-link status (the mention is broader than the knowledge base, several candidates). No name structure sets it: the planner decides from the shortlist and the prompt's context (decision 31, [S2](../docs/stages/S2-planner.md)).

## Identity

| Name | Meaning |
| --- | --- |
| `folder_id` | path of a knowledge-base folder relative to the knowledge-base root, forward slashes, kept byte for byte |
| `concept_id` | `concept:` plus the SHA-256 hex of the UTF-8 bytes of the normalised concept name (below) |
| `image_id` | `sha256:<hex>` of the image bytes. Identical files in several folders are one image |
| `snapshot_id` | id of one immutable knowledge-base snapshot ([commit protocol](#commit-protocol-and-snapshots)) |
| `profile_id` | id of an admin profile: one admin system prompt with its manifest; one admin profile per image model and task set: a model may have one profile for both tasks or separate profiles for generate and edit (decisions 4 and 5) |
| `request_id` | id of one enhancement request; key of its trace |

**One concept, many folders (decision 10, R-007).** The same concept name in several folders is one concept: folders `kimono` and `kimono_donut` (whose info file declares `kimono | donut`) both point to `concept_id` of `kimono`. The normalised name is the name after Unicode NFKC and case folding, nothing else: no diacritic folding, no transliteration, no word list (R-015). Cross-language matching comes from aliases ([K4](../docs/stages/K4-aliases.md)) and embeddings. Whitespace around a name is removed when the info-file line is split, not by normalisation. The original spelling is kept as written in `folder_concepts`.

The mass-deletion removal fraction is unmatched removed folders divided by folders in the previous snapshot, or zero if it has none; confirmed moves are not removals. Ambiguous matches count as removals. A folder awaiting K2 naming has `declared_names=[]`, `name_source=null`, and `error='pending_naming'`; K2 clears the error when indexed. `runtime.cpu_workers: auto` resolves at startup to the number of CPUs available to the process via its affinity where available, otherwise `os.cpu_count()`; the resolved count is reported.

## Tables

K5's implemented serving component uses exact blocked numpy search (decision 134), with explicit metric and block-row configuration. Its Arrow metadata contract is [serving_index.v0](schemas/serving_index.v0.schema.json); source-referenced exact/lexical maps and vector shards live inside the snapshot through `SnapshotBuilder.add_file`. Read-only APIs, complete-key search hits, accepted-alias eligibility, validation and resume are specified in [K5-index](../docs/stages/K5-index.md). K5 does not publish or construct a retrieval gateway. No quality or speed is claimed (R-016/R-017/R-009/R-026).

All tables are Parquet or Arrow (R-016, decision 19); embeddings are Arrow. A snapshot is a set of table files plus a manifest. The serving index (exact blocked numpy search and a lexical index over character n-grams) is derived from these tables and rebuilt per snapshot ([K5](../docs/stages/K5-index.md)); it is not a table. Every row computed by a model carries the revisions of the model, the prompt and the schema that produced it, so an artifact is keyed by input content hash plus those revisions (R-017).

| Table | Key | Main columns | Written by |
| --- | --- | --- | --- |
| `folders` | `folder_id` | `info_file_hash`, `declared_names`, `declared_descriptions`, `name_source` (`info_file`, `folder_name`, `model`, `override`, `folder_override`), `error` (empty or the reason the folder is not indexed) | K0; K2 applies name overrides |
| `concepts` | `concept_id` | `normalised_name`, `display_name`, `kind`, `kind_origin` (`override`, `generated`) | K0 (names), K3 (kind) |
| `folder_concepts` | `folder_id`, `concept_id` | `position`, `name_as_written`, `description` | K0 |
| `images` | `image_id` | `size_bytes`, `format`, `width`, `height`, `decode_status`, `near_duplicate_group` | K0, K1 |
| `folder_images` | `folder_id`, `relative_path` | `image_id`, `modified_at` | K0 |
| `image_embeddings` | `image_id`, `embedder_revision` | `vector` (Arrow), `dim` | K1 |
| `concept_text_embeddings` | `concept_id`, `text_kind`, `embedder_revision`, `text_hash` (several aliases of one concept share the first three; T-006, owner T-008 to confirm) | `vector`; `text_kind` is `name`, `description`, `alias` or `card_summary` | K1 |
| `observations` | `context_key`, `image_id`, `concept_id`, `facet`, observer revisions | `facet_group`, `value`, `visibility`, `region`, `view`, `quality`, `origin` | K2 |
| `cards` | `concept_id` (per snapshot) | the fields of [concept_card.v0](schemas/concept_card.v0.schema.json) | K3 |
| `aliases` | `concept_id`, `alias_text` | `origin`, `status`, `collides_with`, revisions | K4 |
| `sync_state` | `stage`, `input_key` | `output_key`, `state`, revisions, model call records of that artifact | K0 to K5 |
| `snapshots` | `snapshot_id` | `parent_snapshot_id`, `state`, `created_at`, manifest hash, `source_scan_summary` | K0 |
| `eval_scenarios` | `scenario_id` | the fields of [eval_scenario.v0](schemas/eval_scenario.v0.schema.json) | G |
| `eval_labels` | `scenario_id`, `label_id` | answer key derived from folder declarations, `tier`, `split` (`fit` or `test`) | G |
| `eval_results` | `run_id`, `scenario_id` | metrics inputs and outputs, response status, `config_hash` | G |
| `request_traces` | `request_id` | `profile_id`, `snapshot_id`, `effective_config_hash`, response `status`, retrieval state, random choices with their seeds, warnings, per-call records ([vlm-request.md](vlm-request.md#stored-with-every-call)), latency | S0 to S6 |

The per-call records live in `request_traces` online and in `sync_state` offline **[P]**, because the brief for this repository lists no separate calls table. `trace_id` in the response envelope names the `request_traces` row of the request.

## Enums

Values are exact strings; the schema files use the same spelling.

| Enum | Values and meaning |
| --- | --- |
| task | `generate` (text prompt), `edit` (text prompt plus one source image in `request.v0`; several in `request.v1`, a draft nothing reads yet, decision 176 and [Multi-source contracts](#multi-source-contracts-designed-not-yet-implemented)) |
| edit kind | `local_edit` (change one object, attribute, region or text), `global_transform` (medium, style, weather, lighting of the whole image), `subject_recompose` (a new image of this subject) |
| link status | `resolved` (one concept), `family` (the mention is broader than the knowledge base, several candidates), `no_match` |
| context fit | `fits`, `neutral`, `conflicts`: how well a candidate suits the prompt's context; orders `family` and variant choices and never empties them (decisions 15, 16 and 69) |
| role | the part a concept plays in this request, assigned by the planner, not taken from the concept kind (R-008): `subject_identity`, `worn_garment`, `location`, `object`, `style_reference`, `other` |
| concept kind | `character`, `clothing`, `place`, `object`, `style`, `other` |
| facet groups | `identity`, `apparel`, `object`, `place`, `style`, `incidental`, `text_in_image`; a facet name is `<group>.<facet>` |
| visibility | `clear`, `partial`, `occluded`, `not_shown`: how well an image shows a facet of a concept |
| stability labels | `curated` (from the info file or overrides), `stable`, `stable_low_diversity`, `varies`, `single_observation`; rules in [K3](../docs/stages/K3-cards.md) |
| delivery channel | `verbalize` (facts as text in the brief), `attach` (knowledge-base image attached for the editor), `live_look` (writer reads a knowledge-base image); decision 14 |
| evidence mode | `cards` (default, decision 13), `live`, `cards_then_live` |
| retrieval state | `disabled` (`rag.enabled` false); `skipped_no_knowledge_needed` (the planner lists no knowledge need); `no_match` (no mention resolved to a concept, or concepts resolved but no fact passed the filters); `retrieved` (at least one fact or image was assembled); `failed` (a knowledge access failed; `retrieval.error` names it with one closed code: `SNAPSHOT_MISSING`, `KNOWLEDGE_FAILED`, `WINDOW_BUDGET_EXCEEDED`, `RETRIEVAL_FAILED` or `KNOWLEDGE_READ_FAILED`, decision 198); `not_reached` (retrieval on, nothing failed, S3 did not run: an S0 refusal before the knowledge boundary or a plan that stopped the request, decision 200). `response_envelope.v1`; `response_envelope.v0` has no `error` and no `not_reached` |
| image role | `source_image`, `user_reference`, `knowledge_reference` (R-012) |
| decode status | `images.decode_status`, written by K1: `ok`, `undecodable` (Pillow cannot read it), `exceeds_max_decode_pixels`, `alpha_refused` (`vision.alpha_policy` is `refuse`), `encode_failed` (the shared resize cannot encode it in `vision.format`); null until K1 reads the image (T-008). `images.format`, `width` and `height` are the stored file's format and pixel size before any orientation; they stay null when Pillow cannot open or decode the file (`undecodable`) and when Pillow refuses an oversized image before its size is known (`exceeds_max_decode_pixels`) |
| fact origin | `info_file`, `override`, `generated` (a model wrote it, with its revision; this includes a concept name and description the `folder_namer` role produced for a folder without a usable info file); a name or description you wrote is never overwritten, with one exception: when a model judges an info file unclear (decision 40) and its names do not fit the images, the model's names are used, your text stays in the record, the sync report lists the folder, and `kb_overrides.yaml` still wins |
| constraint types | see the table below |
| left-out reasons | `OUT_OF_SCOPE` (role-by-facet matrix), `USER_LOCKED` (the user fixed it), `NEGATED_BY_USER`, `UNSTABLE` (label does not allow use), `VARIANT_NOT_CHOSEN`, `BUDGET` |
| warnings | `PARAM_UNSUPPORTED` (field dropped for an engine), `SCHEMA_INVALID` (a response failed its schema), `TRUNCATED`, `SNAPSHOT_MISSING`, `PROFILE_CONFLICT`, `BUDGET_EXHAUSTED` (a cloud backend's request budget is spent and the role continued on the default backend), `VERIFIER_UNCALIBRATED` (inline verification ran without matching evidence under decision 155), `CLARIFICATION_ASSUMED` **[P]** (an ambiguous request was answered with its most plausible reading under `clarification.mode: best_effort`; the trace names the reading, decision 68; enters the envelope schema's enum once accepted) |

**Response status** (R-024): one per response; a truncated or failed output is never `success`.

| Status | Used when |
| --- | --- |
| `success` | the output passed the structural checks and, where the risk rules ran it, the verifier, on the first draft |
| `repaired` | a check failed, a bounded repair ran (`validation.max_repairs`), and every check then passed |
| `degraded` | an output is returned but part of the pipeline was skipped or failed: planner schema failure falling back to the path without knowledge, retrieval failed, or repair exhausted and a fallback output returned; also an ambiguous request answered with its most plausible reading under `clarification.mode: best_effort` (decision 68) |
| `needs_clarification` | only with `clarification.mode: ask` (decision 68): the planner found an ambiguous edit target, or a mention is a family match and `resolution.on_family_match` is `ask` (candidate concepts listed); no prompt is written. Where the candidate targets travel is `design_required` (they are in the trace) |
| `failed` | no usable output: profile does not support the task (decision 5), source image cannot be decoded, backend error, deadline, truncation without a fallback |

**Constraint types** and how each is checked ([S5](../docs/stages/S5-validate.md), R-027, R-015):

| Type | Checked |
| --- | --- |
| `literal_text`, `passthrough_token` | structurally: exact string equality after structural decoding of the output |
| `format` | structurally: parse, schema, limits the profile declares |
| `negation` | by the verifier model; no negation lexicon exists |
| `count`, `relation`, `attribute_binding` | by the verifier; structurally only where the manifest declares a structural field for it |
| `style` | by the verifier |
| `identity_grounding` | by the verifier: every identifying attribute in the output is in the supplied facts or stated by the user |
| `edit_coverage`, `edit_scope`, `edit_strength` | by the verifier: every delta operation present; none outside the delta; the change is stated clearly enough not to be mistaken for the unedited image |

## Artifacts

Each is a JSON document validated against its schema file, or a table row set; the stage document that produces it is named.

- **Intent IR** ([intent_ir.v0](schemas/intent_ir.v0.schema.json), S2): entities with attributes and sources, scene, concept links with status, role and candidates with context fit, knowledge needs, the constraint ledger, literal texts, passthrough tokens, and for edit the targets, delta, preserve list and dependent effects.
- **Constraint ledger** ([constraint_ledger.v0](schemas/constraint_ledger.v0.schema.json), S2, S5): one entry per user constraint with id, type, the source span of the prompt and the entity it binds to.
- **Knowledge Pack** ([knowledge_pack.v0](schemas/knowledge_pack.v0.schema.json), S3): facts with stability label and origin, selected images with channel and facets, items left out with a reason, and every random choice with its seed.
- **Enhancement Brief** (no schema; text rendered from template `brief_v0` per profile manifest, S4): the user's prompt verbatim, delimited knowledge data, constraints, and for edit the delta and preserve list. See [prompts/README.md](../prompts/README.md).
- **Response envelope** ([response_envelope.v1](schemas/response_envelope.v1.schema.json), S6; v0 stays for stored envelopes): status, output, attachments, source binding, retrieval state, snapshot and closed error code, warnings, trace id. The output carries no citations, warnings or debug fields.
- **Image observation** ([image_observation.v0](schemas/image_observation.v0.schema.json), K2): stored document assembled by code, with image/concept identities, provenance and artifact-bound `context_key` (the 64-character full image-call cache key). Model output uses [observer_reply.v1](schemas/observer_reply.v1.schema.json): concept positions, view, finite quality and facet/value/visibility/region data, never pipeline identities. Code qualifies each dot-free facet name with its group. The v0 observer schema and prompt remain as superseded files. One bundle/checkpoint covers its exact concept-name set and all per-concept rows. Distinct contexts for shared image/concept/facet rows remain distinct in the table primary key. The draft table requires rebuilding older observation tables without context keys; no context is guessed.
- **Concept card** ([concept_card.v0](schemas/concept_card.v0.schema.json), K3): required summary, per-facet observed values with distinct-image support counts, a stability label, variant groups, curated statements, lint findings and revisions. The model returns only [card_builder_reply.v0](schemas/card_builder_reply.v0.schema.json): local positions for semantic value/view groups and lint, a proposed kind and summary. Code validates exact partitions/references and writes every count, stability label, evidence ID and revision. K3 returns rows and checkpoints without publishing; its caller supplies the target snapshot ID.
- **Card absence and lint provenance (P3 fix round 1):** stored `kind.value` admits explicit null, records code-owned `KIND_NOT_GIVEN` and never defaults to `other`; a human type override remains authoritative. Null has no generated kind claim in the concept table. Every model-authored lint code is prefixed by code with reserved `model:`; code-owned findings use unprefixed codes. Semantic grouping keys contain only the positioned payload and model/prompt/reply-schema identities, while exact source hashes and current evidence/document provenance remain separate. An intact committed reply survives a later card derivation/storage failure.
- **Profile manifest** ([profile_manifest.v0](schemas/profile_manifest.v0.schema.json), P): configuration that describes one system prompt; drafted by a model, confirmed by the admin (decision 6). A conflict may cite `config_keys`; the lint checks them against the resolved configuration. The profile bundle `<profiles.root>/<profile_id>/` also holds `golden/<id>.yaml` (one [golden_case.v1](schemas/golden_case.v1.schema.json) per id in `golden_tests`, with `request.retrieval` and `expect.grounding`; a `golden_case.v0` file is refused by name, T017-FIX3), the optional `output_schema.json` (the writer's output schema, `$id` equal to `output_schema_id`, refused when `output_schema_id` is `none` or the id is also a project contract, decision 194), `golden_results.json` (per case: id, status, passed, the judging `codes` (`GOLDEN_STATUS_UNEXPECTED`, `GOLDEN_GROUNDING_DEGRADED`, `GOLDEN_NOT_GROUNDED`, `GOLDEN_RETRIEVAL_NOT_DISABLED`), envelope sha256, the envelope warning codes, a runner failure's class and code, and `diagnostics`: per-stage states, error codes, the planner cause, call statuses, fallback attempts, fatal codes and probe event reasons from `GoldenAdapter.run_case`, `null` for a runner without it; codes only, never reply, prompt or knowledge-base text, R-026, T017-FIX) and `activation.json` (written by `profile check`: sha256 of the `system_prompt.txt` bytes and of the `manifest.yaml` bytes, the addendum id and hash, a sha256 over every cited `config_keys` entry and its resolved value, a sha256 over every golden case file and the image it names, the golden results hash, `schemas_sha256` (the sha256 of each schema file the check applies: `profile_manifest.v0`, `golden_case.v1`, `response_envelope.v1`, `profile_validator.v0`), `output_schema_sha256` (`sha256:` of the `output_schema.json` bytes, `null` without one), `check_code_sha256` (a sha256 over the normalized AST of `config.py` (docstrings dropped, no line or column attributes, so a comment, docstring or whitespace edit keeps it), the bytes of the other P modules that decide the verdict and the PyYAML, jsonschema and Python versions) and the runner identity), plus `check.lock`, the OS lock file of the bundle that `profile register`, confirmation and `profile check` hold while they write. A profile is active only while `activation.json` matches the current files, the cited configuration values, the golden inputs, `golden_results.json`, the schemas and the check code. YAML in `manifest.yaml`, `[META]` and golden cases must not hold a raw U+0085, U+2028 or U+2029 (YAML 1.1 reads them as line breaks, YAML 1.2 as text); the escapes `\N`, `\L` and `\P` are accepted, and recursive aliases are refused. Confirmation by code rewrites only a manifest in the form it writes itself; a hand-edited one (comments, anchors, merge keys, tags, other quoting) is confirmed by hand. `meta`, in `[META]` and in the manifest alike, follows one scalar contract: only `true`, `false` and decimal numbers without a leading zero are typed, and every other plain value is the string as written; `profile check` never rewrites the confirmed manifest, and `profile register` never replaces an existing one.
- **Runtime addendum** (no schema; text): generated from configuration and appended after the admin's system prompt only when a conflict is resolved as `config_wins`; versioned and shown in the trace. The effective prompt is the admin's text plus the addendum, and it is never called the admin's original prompt. Written by P.

K4's model-facing [alias_generator_reply.v0](schemas/alias_generator_reply.v0.schema.json) contains only alias text and nullable language/script; pipeline identity and provenance are never model output. The stored [alias_set.v0](schemas/alias_set.v0.schema.json) now allows a nullable language label and explicit null `model_revision`/`prompt_id` for author-only provenance. Generated rows require nonempty revisions from actual call records; prompt content hashes live in checkpoint revisions. No producer existed before this T-011 change. K4 validates a stored alias set for each row so author rows retain null provenance even alongside generated rows. Exact author/generated duplicates keep the author row. Canonical names and aliases participate in NFKC/casefold collision ownership; only aliases receive `pending_review`, listing other concept IDs, and collision lint. Such rows cannot be used for automatic matching. Collision ownership, author precedence, author-only provenance and the phase split are accepted (decision 136); K5 and serving integration remain later phases.

Also in this folder: [verifier_result.v0](schemas/verifier_result.v0.schema.json) (S5), [info_assessment.v0](schemas/info_assessment.v0.schema.json) and [folder_naming.v0](schemas/folder_naming.v0.schema.json) (K2 naming step), [eval_scenario.v0](schemas/eval_scenario.v0.schema.json) (G), and the runner's closed documents (T-016 P4, G): [eval_result_metrics.v0](schemas/eval_result_metrics.v0.schema.json) (the `metrics` of an `eval_results` row), [eval_judge_pack.v0](schemas/eval_judge_pack.v0.schema.json) (pack header, item and unblinding key) and [eval_judge_scores.v0](schemas/eval_judge_scores.v0.schema.json) (one scorer's file; no free-text field).

## Info file and knowledge-base input

P3 fix round 1 records `SHADOWED_NAME_OVERRIDE` when a folder name shadows a different concept-name `name`. Same-ID folder spelling remains membership-local, with canonical display preserved and `FOLDER_NAME_SPELLING_DIFFERENCE` lint. A folder selector assigning a nameless/different-source folder to an occupied concept is still refused, precisely as `FOLDER_TARGET_OCCUPIED`; admitting deliberate assignment needs a later user decision. Naming lint metadata is checkpointed/reported and does not affect unrelated semantic grouping keys.

Decisions 9, 10, 30 and 31, R-007. Details for authors: [kb/README.md](../kb/README.md).

- **Line 1** of the info file holds the concept names and **line 2** the descriptions, both split by `knowledge.concept_separator` (default the vertical bar) and matched by position.
- Decode with the configured encoding and remove one leading BOM. Only CR, LF and CRLF separate lines; every other character is data for line splitting. NEL, U+2028, U+2029, VT, FF, FS, GS and RS are kept even at token edges.
- Every parsed info file is judged by one text-only `info_assessor` call that returns [info_assessment.v0](schemas/info_assessment.v0.schema.json) (`usable`, reason; decision 40). Without a usable info file a model names the folder (decisions 30, 39): one `folder_namer` call receives images drawn at random from the folder (`knowledge.naming.sample_size`, default 5) with the folder name and any unusable info-file text as hints that may be wrong, and returns [folder_naming.v0](schemas/folder_naming.v0.schema.json): whether the name fits, the concept names, a one-sentence description for each and a reason. `folders.name_source` is `info_file`, `folder_name` (the model judged the folder name to fit and used it as given) or `model` (the model proposed the names); both model-made cases carry origin `generated`. The step runs in [K2](../docs/stages/K2-observe.md). Folder names are used as given and need no special characters; nothing splits them. "Folder name" is the last path segment of `folder_id` **[P]**.
- **Line 2 that does not split into as many parts as line 1 has names** (for example one paragraph that describes every concept) does not stop the folder (decision 59): a model assigns the text of the line to each name. The line stays verbatim in `folders.declared_descriptions`; each assigned description is recorded with origin `generated`, because a model made the assignment; the sync report lists the folder. In [info_assessment.v0](schemas/info_assessment.v0.schema.json), `assigned_descriptions` is required-nullable; for a usable file with a nonempty mismatched second line it contains `{position, description}` entries covering every declared zero-based name position exactly once. Otherwise it is null. Neither naming response schema accepts pipeline-owned identifiers: code adds folder id, sampled image ids and provenance to its checkpoint artifact/report. Assessment disabled with a parsed description mismatch refuses before any model call (T-010 PM answers).
- **Files read in a concept folder** (decision 60): the info file and the files whose extension is in `knowledge.image_extensions`. Every other file, for example JSON files left by another pipeline, is ignored and never read.
- Every image in a folder shows all concepts the folder declares; observations record where that is not true.
- An optional `kb_overrides.yaml` at the knowledge-base root (`knowledge.overrides_file`) is keyed by the **source concept name**, matched by NFKC/casefold identity. [kb_overrides.v0](schemas/kb_overrides.v0.schema.json) validates five optional keys: `name` (one nonblank concept name), `concept_type` (existing kind enum), and string arrays `aliases`, `facts`, `suppress`. `{old_concept_name: {name: corrected_name}}` replaces that known concept in every folder membership after raw naming and before observation. Code derives the corrected concept ID, records name source `override`, and checkpoints the exact-file hash/data and applied mapping. Human declared names/text remain verbatim; physical folders never move. Other keys apply to the replacement concept. Chained/ambiguous renames, normalized target collisions and implicit merges are refused; unmatched sources create nothing and are reported in lint. Removing/changing the override reconstructs names from raw K0/naming results. Unknown fields, unsafe YAML and normalized source-key collisions are refused. Missing file or explicit `none` means absent. Unfaceted `facts` stay curated statements, not invented facets; `suppress` removes the indicated facet assertions. Aliases are retained for K4, with no alias-table write in K3 (decisions 125, 130).
- For an individual folder, use the explicit form `{concepts: {old_name: {...}}, folders: [{folder_id: relative_path, name: corrected_name}]}` (decision 131). Both sections are required; empty sections are valid. This form reserves no concept-name key: legacy concepts literally named `concepts` or `folders` remain valid, because legacy values are override objects and the new `folders` section is an array. Folder paths exactly match K0's relative forward-slash `folder_id`, with no case or Unicode normalization; absolute, backslash and empty/dot/parent-segment spellings are refused. An unknown path is lint only. Within the selected folder, its name wins over a concept-name `name`; other memberships retain the concept-level name. Source-concept curation follows the resulting memberships. A known nameless folder gains one membership with no invented description. A folder with several raw concepts is refused as ambiguous, since the selector identifies no source membership. Conflicting names for one exact folder path, normalized targets belonging to distinct source concepts or nameless folders, and ambiguous curation selectors are refused; identical repeated folder entries are idempotent. Code uses the existing concept identity function and records `folder_override` in the folder row, rename report and applied checkpoint. Changing/removing the selector reconstructs the applied view from raw K0/naming results, before observation; no model naming cache is overwritten.

## Intake contract (S0)

`request.v0` accepts a verbatim string prompt, an explicit generate/edit task, optional admin profile ID,
one optional base64 source image and typed overrides keyed by exact dotted configuration paths. A several-image `request.v1` is designed, not implemented ([Multi-source contracts](#multi-source-contracts-designed-not-yet-implemented)).
`request_overrides.allowed_keys` is required in a configuration profile; an empty list permits no overrides.
Every allowed path must already exist and name a leaf; the policy cannot authorize changing itself.
Overrides are copied into an effective configuration and fully revalidated before any profile or knowledge access.
They never come from prompt text. Deployment overlays retain their separate deployment-only restrictions.

`intake_result.v1` (T017-FIX3; v0 stays for stored results, the only change is the envelope reference) describes the serializable payload of the library result. The live checked profile and
snapshot lease stay outside JSON. `ready` means intake completed, not that enhancement succeeded.
Intake retrieval state `pinned` precedes the later stages' response retrieval state. Missing snapshots produce
`degraded`, retrieval `failed` and `SNAPSHOT_MISSING`. A refusal carries `{code, message}` in `error` beside
a `response_envelope.v1` with status `failed` and null output, whose retrieval is `disabled` with retrieval off,
`failed` with `KNOWLEDGE_FAILED` or `SNAPSHOT_MISSING` at the knowledge boundary, else `not_reached`; diagnostics never enter output.
The caller closes the result (or uses its context manager) only after downstream stages finish.

`vision.max_decode_pixels` is S0's independent, required positive decode limit; it never falls back to a
VLM backend. `runtime.intake_deadline_s` is required positive finite seconds, checked with a monotonic clock
at stage boundaries. It does not preempt an in-progress filesystem or Pillow call. Both example profiles
retain `deploy_required` and `fit_from_bench` respectively until evidence supplies their values.

## Candidate shortlist contract (S1)

`candidate_shortlist.v0` contains the pinned `snapshot_id`, stage-local `state` (ready/failed),
`candidates`, structural `warnings` and nullable `{code, message}` error. Each unique-concept entry
contains canonical name, nullable known kind/description, match source and half-open code-point
`matched_span: {start, end, text}` in the original prompt. Runtime validation also checks span equality
and unique IDs. Description is the first non-null membership description in stable folder/concept order,
with whitespace collapsed structurally to one line. JSON value evidence covers its raw source token.
An empty ready list is valid; budget/index failures produce an empty failed list and the request continues
without knowledge. Overlong dense queries are skipped with a span-bearing `DENSE_QUERY_TOO_LONG` warning.
Query-only template/length fields never enter K1's offline reads or change the stored embedding space.

## Planner contract (S2)

The planner model returns `planner_reply.v0`: concepts, variant groups and variants appear only as zero-based
positions into the shortlist data the request sent (each entry with name, kind, description and the card's variant
groups from the same pin), never as identifiers, seeds or picks. Code checks every position, entity reference and
verbatim span (link mentions, the mentions of `source: user` entities, constraint spans, literal texts and passthrough
tokens are prompt substrings, each text inside its span; an entity read from the source image is not prompt text) and
that every integer is a native integer (`1.0` is refused); a reply that fails the schema or a check is retried once, then S2 is `degraded` without a plan
(`PLANNER_REPLY_INVALID`). A failed call (HTTP error, refusal, truncation, budget stop) is `failed` without retry,
with `PLANNER_CALL_FAILED` and the client's code in `error.cause` (for example `PROBE_MALFORMED`, `MODEL_HTTP_ERROR`).
A `generate` request sends `planner_reply_generate.v0`: `planner_reply.v0` with `edit_kind` `null` and `maxItems: 0` on
`edit_targets`, `delta`, `preserve` and `dependent_effects` (a test derives it from `planner_reply.v0`); `edit` sends
`planner_reply.v0`. Whether llama.cpp's grammar enforces `maxItems` is not verified; client validation refuses a
non-empty list either way (T017-FIX).
Code builds `intent_ir.v0` inside `plan_result.v1` (`plan_result.v0` plus the optional `error.cause`, T017-FIX): a family link takes the code's pick (keep `fits`, else `neutral`,
else all; uniform with a per-request seed, or the configured integer `resolution.random_seed`), recorded in
`selections` when `resolution.trace_choice` is true; a resolved link is never changed by ratings (decisions 15, 69).
`variant_ratings` carries the planner's ratings or named variant per card variant group of the chosen concept for S3
(decision 16). The second-chance lookup reruns S1 on one mention's text over the same pin, then the planner once
more, at most `planner.second_chance_rounds` times, and only when grounding was available: a failed S1 result
(decision 143) or a failed card read continues without knowledge-base grounding, adds the warning `RETRIEVAL_FAILED`
and runs no second chance; a ready but empty shortlist still can. With retrieval off, S2 receives no shortlist and touches no
knowledge path. `planner.catalogue_in_context: true` and `edit.source_entity_linking: true` are refused at start
(not implemented); `resolution.on_family_match: ask` requires `clarification.mode: ask`.

With two or more source images the planner contract changes ([Multi-source contracts](#multi-source-contracts-designed-not-yet-implemented), designed, not implemented): everything above is the one-image path.

Role image sources: `vlm.roles.<name>.image.source` is `source_image`, `user_reference`, `knowledge_reference`,
`edit_source_only` (the request's source image) or `edit_source_and_live_look` (that plus knowledge images); the
model client refuses an image whose role the source does not name, before any request. Each backend's
`image.max_side` (decision 144) caps the longest side of images sent through it; a chat call uses the smaller of the
role's side and the backend's. Embedder backends must keep `none` until K1's cache identity includes the cap.

## Knowledge assembly contract (S3)

S3 (`assembly.py`, T-014 P2) runs only with retrieval on, makes no model call, and runs only on a `ready` or
`degraded` plan that has an Intent IR. It reads only the cards S0 pinned, plus the image tables when it attaches.
It returns `assembly_result.v0`: `retrieval_state`, `pack` (`knowledge_pack.v0`) and `error`. The pack is null and
the error is non-null exactly when the state is `failed`: no pinned snapshot (`SNAPSHOT_MISSING`) or a failed
table read (`KNOWLEDGE_READ_FAILED`). With no needed facet the state is `skipped_no_knowledge_needed` and nothing
is read.

For each `resolved` or `family` link with a role, the card facets the plan needs are considered, and so are the
card's unfaceted curated statements.

- **One reason per considered fact.** Each fact is kept or left out exactly once. The first reason that applies
  wins, in this order:
  1. `OUT_OF_SCOPE`: the facet group is not in `assembly.scope_matrix[role]`, plus `apparel` for
     `subject_identity` under `assembly.default_outfit: include`. Curated statements are always `OUT_OF_SCOPE`,
     with `facet: null`.
  2. `NEGATED_BY_USER`: a `negation` constraint binds the link's entity.
  3. `USER_LOCKED`: the entity has a `source: user` attribute of the same facet. A link without an entity uses
     the scene's user attributes.
  4. `UNSTABLE`: the label is `stable_low_diversity` or `single_observation`.
  5. `VARIANT_NOT_CHOSEN`
  6. `BUDGET`
- **Variants.** A `varies` facet keeps one variant: the variant the user named, else a pick by code from the
  planner's ratings under `resolution.on_variant_unspecified: context_then_random`. Unrated variants count as
  `neutral`. Under `omit` no variant is kept. A random pick is recorded in `selections` when
  `resolution.trace_choice` is true. A `varies` facet without a variant group keeps nothing.
- **Budget and order.** `assembly.fact_order: need_then_support` orders the kept facts by link order, the plan's
  facet order, support count descending, then value. `max_facts_per_concept` counts across links, and
  `max_facts_per_request` counts across the pack.
- **Fact identity.** A fact id is `fact:` plus the SHA-256 of `[snapshot_id, link_index, concept_id, facet,
  value]`. A curated statement adds its folder.
- **Images.** Images are attached only for `edit` with `edit.attach_kb_reference_images`. Selection is greedy
  coverage of the kept facts over their own supporting images, which must decode and have a `clear` or `partial`
  observation. Ties go to coverage, then `clear`, then quality, then a single-concept folder. At most one image
  per near-duplicate group, capped by `assembly.max_images_per_concept` and `edit.max_kb_reference_images`. The
  channel is `attach`, and the note is the JSON list of `{facet, value}` pairs of the kept facts the image supports
  (the card's own per-value image ids); observation rows only rank images, so a value left out as a variant or by
  budget never reaches the note, and `validate_assembly` checks it.
- **Text stays data.** All strings are copied unchanged (R-026).
- **Deferred evidence modes.** `rag.evidence_mode` values `live` and `cards_then_live` are refused at start until
  live reading and the on-demand observer question exist.

## Profile contract

### Structural validator artifacts (T-015 P1)

`manifest.validator.checks` remains an ordered list of names. A parameterized name resolves to `validators/<name>.json` inside that profile bundle, validated against `profile_validator.v0`: an exact `id`, a recognized structural `operator`, explicit `parameters`, and optional admin-confirmed `fallback` metadata. Unknown names/operators, missing parameters, duplicate names and unsafe paths/selectors refuse before generation. No parameter comes from parsing system-prompt prose. Parameter-free `single_line` and `single_line_minified_json` work without files. Operators are `json`, `single_line`, `minified_json`, `keys`, `string_length`, `number_range`, `exact_text` and `schema`; selectors are JSON Pointers, lengths count Unicode code points, and numerical range endpoints are inclusive. Literal equality is exact, without normalization. JSON decoding rejects duplicate keys, nonfinite constants, overflowed floats and nesting beyond 128 containers.

Profile activation binds artifact bytes, referenced validator schemas, the validator contract, check code, writer prompt ID and exact template bytes, and the production runner revision. Golden execution uses the captured brief. Publication and resolution compare those inputs, so changed artifacts or prompt bindings require a new profile check. Golden outputs undergo the declared structural checks. Fallback metadata describes a template plus the sole input variable `original_prompt`. S5 constructs and validates it without another model call. A usable minimal fallback has that insertion and one unambiguous bound template.

S4 returns `writer_result.v1` (`writer_result.v0` plus `effective_system_hash`, the `sha256:` hash of the system texts the call sent, or null when no call was made; T017-FIX): stage state `draft` or `failed`, the profile payload or null, separate warnings/error and input hashes, plus closed call entries `{status,record_json}`. `record_json` retains the existing client's diagnostic record as JSON text without introducing an open control object into the new schema. A failed, refused, truncated or schema-invalid result has no accepted draft. S4 does not assign a final response status or repair output.

Two things are called a profile; see [Terms](#terms). This section describes the configuration profile.

Configuration version 2 (`enhancer_config.v1`) requires `validation.verifier.calibration_ref` and a boolean `validation.verifier.require_calibration` for enhancement requests. The base example declares `deploy_required` for that boolean, the dev example sets it to false and the evidence reference remains `fit_from_gt`. Version 1 remains loadable for earlier commands. A version-1 partial deployment or extends overlay of an explicit version-2 base inherits that base's contract without injecting operational values. Full-configuration references are expanded locally before dotted read validation. K3-RETRY (decisions 183 and 188) adds three required keys to the draft `enhancer_config.v0`, each a positive integer or a sentinel: `observe.reply_attempts`, `cards.reply_attempts` and `vlm.backends.<name>.probe.image_attempts`. The examples carry `fit_from_bench` (measured on the machine, by the seed experiment) until that experiment writes the value; a sentinel is refused by every command that reads the key, and a value above 1 also needs an integer `sampling.seed` on the role and a provider that maps `seed`.

S5 returns closed `validation_result.v1` (its call entries follow `writer_result.v1`) with one status, profile output or null, verdicts, bounded repair count, fallback and call diagnostics, and monitoring selection. Writer and verifier may share a model. Both roles require pinned reachable routes and matching call provenance. True requires `verifier_calibration.v0` evidence matched to the model routes, settings, prompts, ledger and human-checked report. False permits inline gating without matching evidence and adds `VERIFIER_UNCALIBRATED` to the trace and public envelope. Monitoring always requires matching evidence. S6 gives fatal deadlines precedence, returns the profile payload in `output`, and records both configured role model IDs and any actual routes separately in the trace. Edit requests with knowledge attachments need retained images in the pinned snapshot: intake refuses (`KNOWLEDGE_FAILED`, message directing the operator to republish with `kb sync`) a snapshot whose layout marker or per-image manifest entries are missing or differ from the `images` table, and the pipeline re-reads and hash-checks the selected `attach` images right after S3, before S4, with the same refusal. With no published snapshot at all the request stays degraded (`SNAPSHOT_MISSING`) because S3 selects nothing and S4 promises no attachment. The production `Pipeline` and profile `GoldenAdapter` share the coordinator. Callers acknowledge delivery and close its bounded trace and monitoring lifecycle.

Version 2 merges closed `pipeline_runtime.v0` into its runtime reads. Request deadline and trace retention, capacity, worker and drain settings are required. Monitoring capacity, workers and drain are read only when sampling is positive. Durations must fit their clocks or timestamps, and expiry must be representable before trace persistence and snapshot holds. The trace worker setting bounds the executor; repository appends are serialized. See [S6](../docs/stages/S6-envelope.md).

- **Full configuration profiles** define every key: `schema_version`, `status`, `purpose`, `knowledge`, `sync`, `embedding`, `observe`, `cards`, `aliases`, `index`, `rag`, `candidates`, `planner`, `resolution`, `clarification`, `assembly`, `edit`, `creativity`, `writer`, `validation`, `vision`, `request_overrides`, `profiles`, `vlm`, `runtime`, `storage`, `serving`, `evaluation`. A configuration profile may `extends:` another and override any key (the dev profile does).
- **Deployment overlays** may override only deployment values: the `storage`, `runtime` and `serving` blocks and, inside `vlm.backends`, the endpoints, the served model identity, concurrency and the engine-specific request body; never a role, prompt or fitted value. A dev profile that `extends` the full profile may override any key. The merged profile is validated, then hashed.
- Every key is required after merging; an unknown key is refused, and so is a missing one.
- Relative paths supplied to `load_config` and `load_profile` start at the project root. Resolve `extends` and `overlay_of` relative to the referring file, refusing cycles. Merge mappings recursively, replacing scalars and lists, in base, derived, then explicitly selected overlay order. `overlay_of` refers to the overlay's base; overlay restrictions still apply. Configuration and manifest `schema_version` must be `1`. Schema IDs resolve to `contracts/schemas/<id>.schema.json`; unknown versions and IDs are refused.
- The literal string `none` means an optional setting is off or is not sent; it is not silently converted to YAML null. For `vision.max_image_side`, it keeps the original image (decision 84). `computed_at_registration` is retained for registration; `from_profile_manifest` is a per-request reference resolved from the loaded manifest, with unresolved references refused.
- **Sentinels** mark values not known yet; a run refuses a sentinel only in keys its stages read: `fit_from_gt` (fitted on the evaluation set), `fit_from_bench` (measured on the machine), `fit_from_pilot` (from a pilot run), `pin_required` (exact model revision), `deploy_required` (set per machine), `design_required` (decided by the implementing task with evidence), `ask_user` (a paid-call budget). Only `ask_user` is ever a question for the user. No threshold is typed by hand (R-015).
- **YAML 1.1 pitfall:** parsers read unquoted `off`, `on`, `yes`, `no` as booleans. Enum values are quoted and the loader parses with a schema.
- VLM roles follow [vlm-request.md](vlm-request.md) and [vlm_role.v0](schemas/vlm_role.v0.schema.json).

### Settings owned by later tasks

Missing and unknown-key validation covers the keys defined in the configuration schema. The owning stage adds the following settings and their schemas and examples when it is implemented:

| Setting without a defined key | Owner |
| --- | --- |
| Embedding backend, dimension, batch size (K1); devices belong to the embedding server's launch (`runtime.gpus`, `runtime.vram_budget_gb`, the `gpu-live-run` plan), not to a client key | T-008 |
| Intake runtime deadline (S0) | Defined by T-013: `runtime.intake_deadline_s` |

## Commit protocol and snapshots

`kb sync` retains original bytes once per distinct scanned image under a hash-derived snapshot name. Both `sync.retained_images.max_image_bytes` and `sync.retained_images.max_total_bytes` are required positive deployment limits; the latter counts unique images. A copy that cannot be read or whose size or SHA-256 differs from the scan fails publication before `CURRENT` moves. These limits and the retention layout participate in sync identity.

A stage result of many files is committed **data first, manifest last**: the manifest lists keys, sizes and checksums, and a reader trusts only keys the manifest lists. A snapshot is built beside the current one, validated, and published by changing the `CURRENT` pointer; older snapshots stay for `kb rollback` until `kb gc` removes them ([K0](../docs/stages/K0-sync.md), [N125](../docs/note/N125-snapshots-and-vector-deletion.md)). A request pins one published snapshot in S0 and uses only that; an unpublished or half-written snapshot is never read, and a missing pinned snapshot gives the warning `SNAPSHOT_MISSING`. Artifact keys are content hash plus tool revisions ([N126](../docs/note/N126-content-addressed-caches.md)). Publish, rollback, pin and garbage collection serialise on one store lock (`storage.lock`); a request holds a lease on its pinned snapshot until the response is sent, and a trace may hold a snapshot until its retention ends; `kb gc` removes neither. `sync_state.state` is `queued`, `done` or `failed`; `snapshots.state` is `building`, `validated`, `published`, `retired`, `deleting` (garbage collection committed to removing it; holds and rollback refuse it), `collected` or `failed`. `storage.lock` values are finite and positive.

## Multi-source contracts (designed, not yet implemented)

Status (MS-1, decisions 176, 179 and 196; MS-2 landed): these are contract drafts and prompt drafts, except `profile_manifest.v1` and `enhancer_config.v2`, which MS-2 loads (the rows marked MS-2 below; manifest `schema_version: 2` and configuration `schema_version: 3`, with the three source image limits `renderer.max_source_images`, `edit.max_source_images` and a backend's `image.max_images_per_call`; the loader refuses a request that would raise a limit, and a version-2 configuration is refused by the request commands with a message to migrate). No other code reads the drafts and no configuration key selects them; MS-3 to MS-7 build them in order (MS-2 manifest and configuration, MS-3 request and intake, MS-4 planner, MS-5 writer, S5 and S6, MS-6 golden cases and evaluation, MS-7 dev smoke). Everything below is designed, not implemented: today an `edit` takes one source image and runs the contracts of the sections above.

**The design in one paragraph.** An `edit` with n source images gives one edited image. The planner, a model (R-015), names `base_source_index`: the image being edited, whose unchanged parts stay (the `whole_edited_image` of decision 73 then means "unchanged parts as in the base image"); the other sources contribute what the prompt takes from them. It is `null` only for `subject_recompose`. The planner tags every edit target, preserve entry and source-read entity or attribute with `source_indexes`, a non-empty list of 1-based positions in request order. Code never maps a word to a number. A request with exactly one source image keeps today's contracts byte for byte, whether it arrives as `request.v0` or as a one-element `source_images`; the new revisions run only when n is 2 or more, a structural count and not a content rule.

| Today (kept, never edited) | New draft | Used when | Written by |
| --- | --- | --- | --- |
| `request.v0` | [`request.v1`](schemas/request.v1.schema.json) | the caller sends `source_images` (`source_image` is gone; both together are refused) | MS-1 |
| `intake_result.v0`, `intake_result.v1` (T017-FIX3) | [`intake_result.v2`](schemas/intake_result.v2.schema.json) | S0 result once MS-3 lands; a `request.v0` becomes a list of one and `request_version` records the shape; its refusal envelope is `response_envelope.v1` | MS-1 |
| `planner_reply.v0`, `intent_ir.v0` | [`planner_reply.v1`](schemas/planner_reply.v1.schema.json), [`intent_ir.v1`](schemas/intent_ir.v1.schema.json) | n is 2 or more | MS-1 |
| `plan_result.v1` | [`plan_result.v2`](schemas/plan_result.v2.schema.json) | n is 2 or more | MS-1 |
| `response_envelope.v0`, `response_envelope.v1` (T017-FIX3) | `response_envelope.v2` | the request was v1 | MS-5 |
| `profile_manifest.v0`, `enhancer_config.v1` | `profile_manifest.v1`, `enhancer_config.v2` | manifest `schema_version: 2`, configuration `schema_version: 3` (loaded since MS-2; `profile_manifest.v0` and `enhancer_config.v1` stay byte-identical and loadable; a manifest of version 2 binds `profile_manifest.v1` in `schemas_sha256` in place of `profile_manifest.v0`, `profiles.py` `check_schemas`) | MS-2 |
| `golden_case.v0`, `golden_case.v1` (T017-FIX3) | `golden_case.v2` | new bundles | MS-6 |
| `eval_scenario.v0`, `eval_judge_pack.v0` | `.v1` | new sets; the shape is not designed yet | MS-6 |

Unchanged: `constraint_ledger.v0`, `knowledge_pack.v0`, `assembly_result.v0`, `writer_result.v1`, `validation_result.v1` and every K-stage schema. The rows MS-1 does not write left it because parallel fix tasks own those files (T017-FIX3: the envelope, golden and profile-manifest files; CLOUD-1: the cloud and model-client code); the later tasks start from the integrated fixes. Prompt drafts, none selected: [`planner_v2`](../prompts/planner_v2.txt), [`brief_v1`](../prompts/brief_v1.txt), [`manifest_drafter_v1`](../prompts/manifest_drafter_v1.txt) ([prompts](../prompts/README.md)).

**Why `plan_result.v2`.** The design note called the multi-source plan result `plan_result.v1`, but `plan_result.v1` already exists (T017-FIX: `plan_result.v0` plus `error.cause`) and is what the one-source path emits today. The multi-source plan result is its successor, `plan_result.v2`: `plan_result.v1` with `intent_ir` pointing at `intent_ir.v1`. The same note's "v0 contracts for one source" therefore reads "today's contracts": `request.v0`, `planner_v1`, `planner_reply.v0`, `intent_ir.v0`, `plan_result.v1`, `brief_v0`, `writer_result.v1` and `validation_result.v1`.

**Why `intake_result.v2`, `response_envelope.v2` and `golden_case.v2`.** The same collision, found after the design note: T017-FIX3, which lands before the multi-source tasks, owns `intake_result.v1` (the refusal envelope under `response_envelope.v1`), `response_envelope.v1` (a closed `retrieval.error` code and the state `not_reached`, decisions 198 and 200) and `golden_case.v1` (the golden retrieval switch). The multi-source successors take the next number: `intake_result.v2` (MS-1), `response_envelope.v2` (MS-5), `golden_case.v2` (MS-6). The design note's version for each of the three reads one lower.

**Reading by version.** Nothing stored is rewritten; readers dispatch.
- A request is `request.v1` when it holds `source_images` and `request.v0` when it holds `source_image` or neither (a `generate`); both fields is a refusal. S0 turns either into one list.
- The stage contracts follow n, the length of that list: 1 keeps today's, 2 or more uses the drafts above. S3 and S4 accept `plan_result.v1` and `plan_result.v2`.
- The response envelope follows `request_version`: a `request.v0` keeps the envelope of today's path (`response_envelope.v0`, or `response_envelope.v1` once T017-FIX3 lands); a `request.v1` gets `response_envelope.v2` (MS-5).
- Stored rows (traces, profile bundles, evaluation sets) are read by the version they record; a row without one is v0 and is never rewritten.

**What a schema checks and what code checks.** A JSON schema cannot see n or compare two fields, so the schemas hold what they can and code holds the rest. The fixtures list the second kind as "schema-valid, code-refused".

| Rule | Schema | Code |
| --- | --- | --- |
| index is an integer of at least 1 (0-based, negative, fractional, string, boolean refused); list is non-empty with no repeats | yes (all five schemas, wherever an index appears) | also native integers (`1.0` is refused, as for every planner integer) |
| each index is at most n; lists ascend; indexes run 1..n without gaps; no two images share bytes | no | yes (MS-3 for the request and intake, MS-4 for the reply); a reply failure takes the existing path: one retry, then `PLANNER_REPLY_INVALID` or degraded |
| `base_source_index` is `null` only for `subject_recompose` | `intent_ir.v1` only; the model-facing `planner_reply.v1` has no `if`/`then`, so the local grammar backend can take it | `planner_reply.v1` |
| an entity or attribute has `source_indexes` exactly when its source is `source_image` | `intent_ir.v1` only | `planner_reply.v1` |
| the task is `edit` and `edit_kind` is not null | yes (`intent_ir.v1`, `planner_reply.v1`) | the choice between v0 and v1 by n |
| edit needs at least one image, generate none, an empty list is refused, both fields are refused | yes (`request.v1`, `intake_result.v2`) | the same, before any decoding |
| the caps: `edit.max_source_images`, `renderer.max_source_images`, the backend's `image.max_images_per_call` | no (they are configuration values, so no `maxItems`) | yes: over any cap is refused (`SOURCE_IMAGES_TOO_MANY`), never truncated |

**`intake_result.v2.envelope`.** It references `response_envelope.v1`, written by T017-FIX3, which lands first. A tree that does not hold that file yet cannot resolve the reference, so the fixture test of `intake_result.v2` is skipped there with a stated reason and is re-run after the merge. `response_envelope.v2`, the multi-source envelope, is MS-5's; which envelope a refused `request.v1` carries is settled there.

**Contract examples.** The design's worked examples live in [tests/fixtures/contracts/multi_source/](../tests/fixtures/contracts/multi_source/), one file per draft schema with valid, invalid and schema-valid-but-code-refused cases, checked by `tests/test_multi_source_contracts.py`.

| Worked example | Where |
| --- | --- |
| 1. One source, today's client: `{"prompt": "đổi áo thành màu đỏ", "task": "edit", "source_image": "<b64>"}` runs `planner_v1`, `planner_reply.v0`, `intent_ir.v0` and `brief_v0` on the same data and gets `response_envelope.v0`. The same request as `"source_images": ["<b64>"]` renders the same brief; only its v1 envelope differs. | [request.v1](../tests/fixtures/contracts/multi_source/request.v1.json) ("one source"), [intake_result.v2](../tests/fixtures/contracts/multi_source/intake_result.v2.json) (a request.v0 migrated to a list of one) |
| 2. Three sources, two knowledge references: "Đặt cô gái ở ảnh 2 vào quán cà phê ở ảnh 1, mặc áo dài Huế, tay cầm ly ở ảnh 3; giữ ánh sáng ảnh 1" gives base 1, the girl `[2]`, the café `[1]`, the cup `[3]`, the target `[1]` and the lighting `[1]`. | [planner_reply.v1](../tests/fixtures/contracts/multi_source/planner_reply.v1.json), [intent_ir.v1](../tests/fixtures/contracts/multi_source/intent_ir.v1.json), [plan_result.v2](../tests/fixtures/contracts/multi_source/plan_result.v2.json) (all built on this request) |
| 3. "The second photo": "the dog in the second photo" gives target `[2]`; "the third photo" with two images goes under `unresolved` with `needs_clarification`; a reply that returns `[3]` anyway is refused by the range check (retry, then degraded). | planner_reply.v1 ("shape only" and the "above n" cases) |

The `brief_v1` data of example 2 adds to `brief_v0`'s data (the profile syntax is `"<source_{n}>"` and `"<ref_{n}>"`; the two groups count from 1 each, and a source is never reference 1). The tokens are data fields: the pipeline writes none into the output, and the admin's system prompt, used verbatim, decides what the writer does with them (R-004).

```json
{"sources": [{"index": 1, "ref": "<source_1>"}, {"index": 2, "ref": "<source_2>"}, {"index": 3, "ref": "<source_3>"}],
 "knowledge": {"attachments": [{"position": 1, "ref": "<ref_1>"}, {"position": 2, "ref": "<ref_2>"}]}}
```

The envelope of example 2 (`source_bindings` with indexes 1..3, `attachments` with `index` 1 and 2 and role `knowledge_reference`) is part of `response_envelope.v2`, written by MS-5, and has no example here yet. Stage behaviour: [S0](../docs/stages/S0-intake.md), [S2](../docs/stages/S2-planner.md), [S4](../docs/stages/S4-writer.md), [S6](../docs/stages/S6-envelope.md).

## Schemas

JSON Schema draft 2020-12 files in [schemas/](schemas/), versioned in the name. Versioning rule (open point 5 of T017-FIX, PM decision pending user review): a draft configuration schema (`enhancer_config.v0`) takes additive keys in place, because the loader validates the resolved configuration on every start and no stored artifact is validated against it (it is not in `schemas_sha256`); a stage or stored-artifact schema (stage results such as `plan_result`, `writer_result` and `validation_result`, and the schemas `profile check` hashes into the activation record, `schemas_sha256` over `profile_manifest.v0`, `golden_case.v1`, `response_envelope.v1` and `profile_validator.v0`, `profiles.py` `CHECK_SCHEMAS`) gets a new version for any change, so an artifact stored under the old version keeps its meaning. All are drafts: `enhancer_config.v0`, `request.v0`, `intake_result.v1`, `candidate_shortlist.v0`, `planner_reply.v0`, `planner_reply_generate.v0`, `plan_result.v1`, `intent_ir.v0`, `constraint_ledger.v0`, `knowledge_pack.v0`, `assembly_result.v0`, `image_observation.v0`, `concept_card.v0`, `verifier_result.v0`, `profile_manifest.v0`, `golden_case.v1`, `response_envelope.v1`, `alias_set.v0`, `info_assessment.v0`, `folder_naming.v0`, `eval_scenario.v0`, `vlm_role.v0`; and the multi-source drafts `request.v1`, `intake_result.v2`, `planner_reply.v1`, `intent_ir.v1` and `plan_result.v2` (MS-1). Superseded files stay for stored artifacts: `plan_result.v0`, `writer_result.v0` and `validation_result.v0` (T017-FIX moved every writer and reader to v1), and `intake_result.v0`, `response_envelope.v0` and `golden_case.v0` (T017-FIX3). Ranges in them are the schemas' own bounds, not engine limits. VLM request fields and engine mapping: [vlm-request.md](vlm-request.md).
