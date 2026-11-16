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
| task | `generate` (text prompt), `edit` (text prompt plus one source image) |
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
| retrieval state | `disabled` (`rag.enabled` false); `skipped_no_knowledge_needed` (the planner lists no knowledge need); `no_match` (no mention resolved to a concept, or concepts resolved but no fact passed the filters); `retrieved` (at least one fact or image was assembled); `failed` (a knowledge access failed) |
| image role | `source_image`, `user_reference`, `knowledge_reference` (R-012) |
| decode status | `images.decode_status`, written by K1: `ok`, `undecodable` (Pillow cannot read it), `exceeds_max_decode_pixels`, `alpha_refused` (`vision.alpha_policy` is `refuse`), `encode_failed` (the shared resize cannot encode it in `vision.format`); null until K1 reads the image (T-008). `images.format`, `width` and `height` are the stored file's format and pixel size before any orientation; they stay null when Pillow cannot open or decode the file (`undecodable`) and when Pillow refuses an oversized image before its size is known (`exceeds_max_decode_pixels`) |
| fact origin | `info_file`, `override`, `generated` (a model wrote it, with its revision; this includes a concept name and description the `folder_namer` role produced for a folder without a usable info file); a name or description you wrote is never overwritten, with one exception: when a model judges an info file unclear (decision 40) and its names do not fit the images, the model's names are used, your text stays in the record, the sync report lists the folder, and `kb_overrides.yaml` still wins |
| constraint types | see the table below |
| left-out reasons | `OUT_OF_SCOPE` (role-by-facet matrix), `USER_LOCKED` (the user fixed it), `NEGATED_BY_USER`, `UNSTABLE` (label does not allow use), `VARIANT_NOT_CHOSEN`, `BUDGET` |
| warnings | `PARAM_UNSUPPORTED` (field dropped for an engine), `SCHEMA_INVALID` (a response failed its schema), `TRUNCATED`, `SNAPSHOT_MISSING`, `PROFILE_CONFLICT`, `BUDGET_EXHAUSTED` (a cloud backend's request budget is spent and the role continued on the default backend), `CLARIFICATION_ASSUMED` **[P]** (an ambiguous request was answered with its most plausible reading under `clarification.mode: best_effort`; the trace names the reading, decision 68; enters the envelope schema's enum once accepted) |

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
- **Response envelope** ([response_envelope.v0](schemas/response_envelope.v0.schema.json), S6): status, output, attachments, source binding, retrieval state and snapshot, warnings, trace id. The output carries no citations, warnings or debug fields.
- **Image observation** ([image_observation.v0](schemas/image_observation.v0.schema.json), K2): stored document assembled by code, with image/concept identities, provenance and artifact-bound `context_key` (the 64-character full image-call cache key). Model output uses [observer_reply.v0](schemas/observer_reply.v0.schema.json): concept positions, view, finite quality and facet/value/visibility/region data, never pipeline identities. One bundle/checkpoint covers its exact concept-name set and all per-concept rows. Distinct contexts for shared image/concept/facet rows remain distinct in the table primary key. The draft table requires rebuilding older observation tables without context keys; no context is guessed.
- **Concept card** ([concept_card.v0](schemas/concept_card.v0.schema.json), K3): required summary, per-facet observed values with distinct-image support counts, a stability label, variant groups, curated statements, lint findings and revisions. The model returns only [card_builder_reply.v0](schemas/card_builder_reply.v0.schema.json): local positions for semantic value/view groups and lint, a proposed kind and summary. Code validates exact partitions/references and writes every count, stability label, evidence ID and revision. K3 returns rows and checkpoints without publishing; its caller supplies the target snapshot ID.
- **Card absence and lint provenance (P3 fix round 1):** stored `kind.value` admits explicit null, records code-owned `KIND_NOT_GIVEN` and never defaults to `other`; a human type override remains authoritative. Null has no generated kind claim in the concept table. Every model-authored lint code is prefixed by code with reserved `model:`; code-owned findings use unprefixed codes. Semantic grouping keys contain only the positioned payload and model/prompt/reply-schema identities, while exact source hashes and current evidence/document provenance remain separate. An intact committed reply survives a later card derivation/storage failure.
- **Profile manifest** ([profile_manifest.v0](schemas/profile_manifest.v0.schema.json), P): configuration that describes one system prompt; drafted by a model, confirmed by the admin (decision 6). A conflict may cite `config_keys`; the lint checks them against the resolved configuration. The profile bundle `<profiles.root>/<profile_id>/` also holds `golden/<id>.yaml` (one [golden_case.v0](schemas/golden_case.v0.schema.json) per id in `golden_tests`), `golden_results.json` (per case: id, status, passed, envelope sha256) and `activation.json` (written by `profile check`: sha256 of the `system_prompt.txt` bytes and of the `manifest.yaml` bytes, the addendum id and hash, a sha256 over every cited `config_keys` entry and its resolved value, a sha256 over every golden case file and the image it names, the golden results hash, `schemas_sha256` (the sha256 of each schema file the check applies: `profile_manifest.v0`, `golden_case.v0`, `response_envelope.v0`), `check_code_sha256` (a sha256 over the normalized AST of `config.py` (docstrings dropped, no line or column attributes, so a comment, docstring or whitespace edit keeps it), the bytes of the other P modules that decide the verdict and the PyYAML, jsonschema and Python versions) and the runner identity), plus `check.lock`, the OS lock file of the bundle that `profile register`, confirmation and `profile check` hold while they write. A profile is active only while `activation.json` matches the current files, the cited configuration values, the golden inputs, `golden_results.json`, the schemas and the check code. YAML in `manifest.yaml`, `[META]` and golden cases must not hold a raw U+0085, U+2028 or U+2029 (YAML 1.1 reads them as line breaks, YAML 1.2 as text); the escapes `\N`, `\L` and `\P` are accepted, and recursive aliases are refused. Confirmation by code rewrites only a manifest in the form it writes itself; a hand-edited one (comments, anchors, merge keys, tags, other quoting) is confirmed by hand. `meta`, in `[META]` and in the manifest alike, follows one scalar contract: only `true`, `false` and decimal numbers without a leading zero are typed, and every other plain value is the string as written; `profile check` never rewrites the confirmed manifest, and `profile register` never replaces an existing one.
- **Runtime addendum** (no schema; text): generated from configuration and appended after the admin's system prompt only when a conflict is resolved as `config_wins`; versioned and shown in the trace. The effective prompt is the admin's text plus the addendum, and it is never called the admin's original prompt. Written by P.

K4's model-facing [alias_generator_reply.v0](schemas/alias_generator_reply.v0.schema.json) contains only alias text and nullable language/script; pipeline identity and provenance are never model output. The stored [alias_set.v0](schemas/alias_set.v0.schema.json) now allows a nullable language label and explicit null `model_revision`/`prompt_id` for author-only provenance. Generated rows require nonempty revisions from actual call records; prompt content hashes live in checkpoint revisions. No producer existed before this T-011 change. K4 validates a stored alias set for each row so author rows retain null provenance even alongside generated rows. Exact author/generated duplicates keep the author row. Canonical names and aliases participate in NFKC/casefold collision ownership; only aliases receive `pending_review`, listing other concept IDs, and collision lint. Such rows cannot be used for automatic matching. Collision ownership, author precedence, author-only provenance and the phase split are accepted (decision 136); K5 and serving integration remain later phases.

Also in this folder: [verifier_result.v0](schemas/verifier_result.v0.schema.json) (S5), [info_assessment.v0](schemas/info_assessment.v0.schema.json) and [folder_naming.v0](schemas/folder_naming.v0.schema.json) (K2 naming step), [eval_scenario.v0](schemas/eval_scenario.v0.schema.json) (G).

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

## Profile contract

Two things are called a profile; see [Terms](#terms). This section describes the configuration profile.

- **Full configuration profiles** define every key: `schema_version`, `status`, `purpose`, `knowledge`, `sync`, `embedding`, `observe`, `cards`, `aliases`, `index`, `rag`, `candidates`, `planner`, `resolution`, `clarification`, `assembly`, `edit`, `creativity`, `writer`, `validation`, `vision`, `profiles`, `vlm`, `runtime`, `storage`, `serving`, `evaluation`. A configuration profile may `extends:` another and override any key (the dev profile does).
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
| Intake runtime deadlines (S0) | T-013 |

## Commit protocol and snapshots

A stage result of many files is committed **data first, manifest last**: the manifest lists keys, sizes and checksums, and a reader trusts only keys the manifest lists. A snapshot is built beside the current one, validated, and published by changing the `CURRENT` pointer; older snapshots stay for `kb rollback` until `kb gc` removes them ([K0](../docs/stages/K0-sync.md), [N125](../docs/note/N125-snapshots-and-vector-deletion.md)). A request pins one published snapshot in S0 and uses only that; an unpublished or half-written snapshot is never read, and a missing pinned snapshot gives the warning `SNAPSHOT_MISSING`. Artifact keys are content hash plus tool revisions ([N126](../docs/note/N126-content-addressed-caches.md)). Publish, rollback, pin and garbage collection serialise on one store lock (`storage.lock`); a request holds a lease on its pinned snapshot until the response is sent, and a trace may hold a snapshot until its retention ends; `kb gc` removes neither. `sync_state.state` is `queued`, `done` or `failed`; `snapshots.state` is `building`, `validated`, `published`, `retired`, `deleting` (garbage collection committed to removing it; holds and rollback refuse it), `collected` or `failed`. `storage.lock` values are finite and positive.

## Schemas

JSON Schema draft 2020-12 files in [schemas/](schemas/), versioned in the name; a change is a new version. All are drafts: `enhancer_config.v0`, `intent_ir.v0`, `constraint_ledger.v0`, `knowledge_pack.v0`, `image_observation.v0`, `concept_card.v0`, `verifier_result.v0`, `profile_manifest.v0`, `golden_case.v0`, `response_envelope.v0`, `alias_set.v0`, `info_assessment.v0`, `folder_naming.v0`, `eval_scenario.v0`, `vlm_role.v0`. Ranges in them are the schemas' own bounds, not engine limits. VLM request fields and engine mapping: [vlm-request.md](vlm-request.md).
