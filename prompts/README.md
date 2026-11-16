# Prompt contracts

These are contracts for the runtime prompts; a prompt marked `written` has its text in `prompts/<prompt_id>.txt`, the others have none yet. Runtime prompts are English (AGENTS.md section 1) and versioned by `prompt_id`; a changed prompt is a new id, and every artifact built with the old id loses its key ([execution](../docs/execution.md), R-017).

Rules for every prompt:

- **Data is delimited.** Knowledge-base text, text read from images, captions and the user's prompt go inside delimited data blocks, never at instruction position (R-026, [N131](../docs/note/N131-image-prompt-injection.md)), because text in data can otherwise act as an instruction.
- **No word lists or per-language rules** appear in any prompt (R-015); the model reads the language it is given.
- **The writer has no pipeline-owned system prompt.** The admin's system prompt is used verbatim; the pipeline adds only the Enhancement Brief as the user message, and a runtime addendum when a conflict is resolved as `config_wins` ([P-profiles](../docs/stages/P-profiles.md)). The admin's text lives in `configs/profiles/<profile_id>/system_prompt.txt` and is never edited.
- Output schemas are in [contracts/schemas/](../contracts/schemas/); request fields in [vlm-request.md](../contracts/vlm-request.md).

## `planner_v0` (S2), status: not written

Role `planner`. Placeholders: `{raw_prompt}`, `{task}`, `{shortlist}` (empty when retrieval is off), `{quote_semantics}` and `{creativity}` from the manifest and configuration. Delimited data: the raw prompt and every shortlist entry. Source image attached for `edit`. Asks for the Intent IR including the constraint ledger, with `resolved`, `family` or `no_match` per mention. Output schema: `intent_ir.v0`. A change invalidates golden tests, the linking experiment E1 results and verifier calibration that used its ledgers.

## `verifier_v0` (S5), status: not written

Role `verifier`. Placeholders: `{ledger_entries}` (only entries the risk rules send to the model), `{output_text}`, `{granted_facts}`. Delimited data: the output under test and the granted facts. Answers per constraint id with a verdict and a quoted span. Output schema: `verifier_result.v0`. A change invalidates the verifier calibration (`fit_from_gt`) and the gating results of E7.

## `observer_v0` (K2), status: written

Role `observer`. One image per call. [System text](observer_v0.txt) is loaded byte-for-byte. A separate JSON user-data object contains `concept_names` (zero-based `concept_position` and name only, no descriptions or pipeline IDs) and `facet_schema` (version and seven groups). Names and text visible in the image are untrusted data; visible text is quoted verbatim as `text_in_image` evidence, never obeyed. Output schema: `observer_reply.v0`; code maps positions and constructs/validates stored `image_observation.v0`. A change invalidates every observation and therefore cards, their embeddings and the index ([K2](../docs/stages/K2-observe.md)); image embeddings stay. CPU double tests prove containment and preservation, not real-model resistance to injection.

## `info_assessor_v0` (K2, first step), status: written

Role `info_assessor`. Text only, one call per folder with a parsed info file. Placeholders: `{folder_name}`, `{info_file_text}`. Delimited data: both, as data and never as instructions (R-026). Asks whether the file gives usable concept names; a missing description alone is not a reason to answer no. No rule or word list makes the judgement (R-015). Output schema: `info_assessment.v0`. A change re-judges every info file; a folder whose verdict changes is renamed or restored and its observations, cards and aliases follow ([K2](../docs/stages/K2-observe.md)).

Text: [info_assessor_v0.txt](info_assessor_v0.txt), loaded byte-for-byte as the system message. Code renders hints, raw info text and names labelled by zero-based position in a JSON user object. The reply contains no folder id. `assigned_descriptions` is required-nullable: only a usable file with a nonempty mismatched second line returns `{position, description}` entries, covering exactly the declared positions. The original line stays verbatim; assigned descriptions have generated provenance. Disabling assessment with a parsed description mismatch refuses the stage before any model call.

## `folder_namer_v0` (K2, first step), status: written

Role `folder_namer`. Several images in one call (a deliberate exception to one image per call, because the question is what the images have in common; leakage between images is the failure mode to measure in E17). Placeholders: `{folder_name}`, `{info_file_text}` (present only when the folder has an info file that cannot be read as names or was judged unclear), `{images}` (drawn at random, `knowledge.naming.sample_size`). Delimited data: the folder name and the info-file text, each labelled as a hint that may be wrong, and the images; text read from an image is data (R-026). Asks whether the folder name fits what the images show, whether the info file's names fit, whether the images belong together, the concept name or names (the folder name itself when it fits, otherwise names the model proposes, several when the images consistently show several concepts), a one-sentence description for each and a short reason. No rule decides whether the name is related (R-015). Output schema: `folder_naming.v0`. A change invalidates generated names and everything computed from them (observations, cards, aliases) and the E17 results ([K2](../docs/stages/K2-observe.md)).

Text: [folder_namer_v0.txt](folder_namer_v0.txt), unchanged system bytes plus a JSON data message and raw `knowledge_reference` attachments. T-009 prepares each image once. The reply never supplies `folder_id`, `sampled_image_ids` or provenance; code writes these into the checkpoint/report. Fitting info names take precedence over a fitting folder-name hint and must be returned exactly; fitting human descriptions stay intact. Invalid replies commit no naming result. More than one image requires proved `multiple_images` capability, not a loop of single-image calls.

## `card_builder_v0` (K3), status: written

Role `card_builder`. [System text](card_builder_v0.txt) is loaded byte-for-byte. A separate JSON user-data object contains concept name, open facet names, observations, views, suppressed facets and curated statements with local positional labels only. All are untrusted data, including OCR-derived text and strings resembling delimiters. The reply uses `card_builder_reply.v0`, partitioning visible observations into semantic values and observations into view groups, proposing a kind, summary and referenced semantic lint. Code validates every position/partition and constructs required-summary `concept_card.v0`, including all counts, labels, IDs and revisions. CPU double tests establish placement/validation, not real-model resistance or quality. A change invalidates cards and the E16 audit; K4/embeddings remain later stages.

## `alias_generator_v0` (K4), status: written

Role `alias_generator`. [System text](alias_generator_v0.txt) is loaded byte-for-byte. The separate JSON data message contains `concept_name`, `descriptions` (distinct assigned descriptions, deterministic order), `card_summary` and configured `languages`. All strings are untrusted data (R-026). Asks for names in the requested languages/scripts and common spellings; an empty result is valid. The ID-free reply schema `alias_generator_reply.v0` contains only aliases with text and nullable language/script labels. Code owns IDs, provenance and collision decisions when constructing `alias_set.v0` rows. Prompt bytes, semantic model/inference settings, schema and languages enter the cache identity. CPU doubles test containment and validation, not real-model resistance or alias quality. A change invalidates aliases and the candidate experiments E1 and E4.

## `manifest_drafter_v0` (P), status: written

Role `manifest_drafter`. Text: [manifest_drafter_v0.txt](manifest_drafter_v0.txt), the system message, loaded as `prompts/<prompt_id>.txt`. The user message is one JSON object rendered by code, so the admin's text is a single JSON string and cannot leave its delimiter (R-026): `profile_id`, `system_prompt_text`, `structure` (template format, `[META]` values and `{{name}}` placeholders found structurally) and `pipeline_policy.configuration` (the resolved `creativity` block keyed by dotted path). Read once at registration. Drafts the manifest and the conflict list; the admin confirms. Output schema: `profile_manifest.v0`. The fixed rules the prompt lists come from R-001 (intent survives unchanged), the missing-evidence rule (P-profiles method step 3; S5 `identity_grounding`; R-010) and R-026 (knowledge and image text are data). A change affects only later drafts; a confirmed manifest stays as the admin signed it.

## `runtime_addendum_v0` (P, read by S4), status: written

Not a role prompt: the header text of the runtime addendum, [runtime_addendum_v0.txt](runtime_addendum_v0.txt). Code appends one JSON line listing each conflict decided `config_wins` with its id, its `profile_rule` and the resolved values of its `config_keys`. Versioned by this id plus the sha256 of the rendered text; `profile check` records the hash, and a different rendering at request time needs a new check. A change invalidates every activation that has a `config_wins` conflict.

## `scenario_generator_v0` (G), status: not written

Role `scenario_generator`. Placeholders: `{folder_declarations}`, `{kind}`, `{task}`. Delimited data: folder names and descriptions. The answer key comes from the declarations, not from the model (R-023). Output schema: `eval_scenario.v0`. A change invalidates generated scenarios and any value fitted on them.

## `brief_v0` (S4), status: not written

Not a model-facing instruction set but the Enhancement Brief template, rendered per profile manifest into the writer's user message. Placeholders: the manifest's template variables, the raw prompt, `{knowledge_pack}`, `{constraints}`, and for edit `{delta}` and `{preserve}`. Delimited data: the knowledge facts, one per line bound to the entity they serve. Quote characters follow the manifest's quote semantics. No output schema: the output follows the profile. A change invalidates golden tests of every profile that renders it and E2, E6 and E8 results.
