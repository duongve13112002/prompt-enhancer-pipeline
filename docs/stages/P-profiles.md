# P Profiles: registration, manifest, conflict lint, activation

**Purpose.** Turn an admin's system prompt into an admin profile the pipeline can use verbatim and check against, with every conflict caught at registration rather than at request time (R-004, decisions 4 to 6). One admin profile per image model and task set: a model may have one profile for both tasks or separate profiles for generate and edit (decisions 4 and 5). A request names its `profile_id`, else `profiles.default_profile_id` applies.

**Input and output.** `system_prompt.txt` (verbatim, hashed, never edited), then a profile bundle under `profiles.root`: the confirmed profile manifest, the conflict list with one decision per conflict, golden test cases and their results, and the activation status. Example: [configs/profiles/_example/README.md](../../configs/profiles/_example/README.md); schema: [contracts](../../contracts/README.md).

**Method.**
1. `profile register`: the `manifest_drafter` model reads the system prompt once and drafts the manifest and the conflict list.
2. The admin confirms or corrects the manifest; the pipeline never infers profile rules at run time.
3. **Conflict lint** compares the profile with the configuration (creativity, validation, the rule that missing evidence is omitted, never invented). Each conflict gets one admin decision: `profile_wins` (the profile's rule applies), `config_wins` (a runtime addendum generated from configuration is appended after the admin's text, versioned and shown in the trace, [S4](S4-writer.md)), or `block_activation`.
4. `profile check` runs the golden tests (at least one case per conflict) and activates the profile when every test passes and no conflict blocks.

**System prompt file format [P].** The manifest declares `template_format`, `sectioned` or `plain`.
- `sectioned`: the file has sections marked `[META]`, `[SYSTEM]` and `[USER]`. The `[SYSTEM]` section is the system message and is used verbatim. The `[USER]` section is the user-turn template that contains the input variable. `[META]` is loader metadata: it is recorded in the manifest and mapped to a runtime parameter only when the backend supports it (for example a thinking switch), never guessed.
- `plain`: the whole file is the system message and the brief is the user message.

**What a manifest declares.** `template_format`; supported tasks (`generate`, `edit` or both); output kind (JSON, tags, prose or other) and its schema or grammar if any; output language (decision 67: free text in any wording, interpreted by the model, never mapped by code; a language the system prompt states wins, else the drafter writes `English` for the admin to confirm or change); the template's input variable the Enhancement Brief fills; for edit, the output semantics; whether the downstream image model accepts reference images and how an image is referenced in text; quote semantics (for example quoted strings are literal text to render); validators for structural checks.

**Decisions and configuration keys.** `profiles.root`, `profiles.default_profile_id`, the admin profile's `status`, `vlm.roles.manifest_drafter`. An edit request sent to a generate-only profile is refused with a clear error, never reformatted (decision 5). No width, height or aspect ratio enters the contract; bounding boxes are the profile's choice (R-005).

**Known conflicts of the example profile.** The lint is expected to report at least:
- "Populate underspecified scenes" and "Built environments need text everywhere" against creativity settings that allow no new entities or text blocks, and against a user's "no text".
- "Quoted strings: each is its own text element": the manifest records quote semantics as literal text, so the brief renders concept names without quotation marks.
- "Banned hedge phrasings" and "Pick ONE and commit" against omitting what lacks evidence: missing identity evidence drops the attribute instead of picking a value.

**Settled readings of the example profile (not conflicts).**
- The user uses this one prompt for both generate and edit (decision 70). The prompt states no edit semantics, so the manifest records the user's: for an edit, the JSON describes the whole edited image, with unchanged parts as in the source image (`edit_output_semantics: whole_edited_image`, decision 73).
- The prompt's aspect-ratio and bounding-box parts (`{{aspect_ratio}}` in the `[USER]` template) are a temporary example and are ignored (decision 70); R-005 stands.

**Compute.** One drafting call per registration, plus golden tests through the full pipeline. Not implemented; nothing measured.

**Depends on.** Nothing upstream; S0, S4 and S5 read the active profile.

**Failure modes.** A drafted manifest that misreads the output format (the admin confirms it); a conflict the drafter misses (golden tests and S5 catch it later, warning `PROFILE_CONFLICT`); a profile changed after activation (a new hash requires a new check).

**Experiments.** Golden tests per profile; E7 uses known conflicts as a gating condition.

**References.** [N101](../note/N101-qwen-image-pe-t2i.md), [N112](../note/N112-prompt-rewriters.md).
