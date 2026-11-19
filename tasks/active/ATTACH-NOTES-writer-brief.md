# ATTACH-NOTES: one note per attached image in the writer brief

Status: implemented and tested in this worktree; not committed (the orchestrator commits). R-IDs: R-001, R-004, R-006, R-009, R-026. Decision 170 item 4.

## Step 0

Unknowns: none that change product meaning. Questions (each with the answer I took; none blocked the work):
1. Where in the JSON block? Answer: a new `knowledge.attachments` list beside `facts` and `images`, mirroring S6's `attachments`, and present only when at least one `attach` image exists, so every other brief keeps its bytes.
2. Position base? Answer: 1-based index in the S6 attachment order. The brief fixes no syntax; the profile's `renderer.image_reference_syntax` stays the writer's business.
3. What if an `attach` item meets `generate` or `attach_kb_reference_images: false`? Answer: refuse with `ValueError` (S3 already refuses it; silently dropping would hide a defect).
4. Re-validate the note in S4? Answer: yes, with the same rule S3 uses (one shared function), so the writer re-reading the pack cannot bypass S3.
5. Edit the docs? Answer: no, proposed text is below, the PM moves it.

Plan, checkable finish: (a) factor S3's note check into `assembly.attached_note`; (b) render entries in `writer.render_brief`; (c) tests in `tests/test_writer_attachments.py`; (d) run the named sets, 10 mutants. Finish: the Done check items below each map to a test.

## Root cause confirmed

- `src/prompt_enhancer/writer.py:81` (before the change) kept only `channel != 'attach'` items in `knowledge.images`, so an `attach` image never reached the brief. `docs/stages/S4-writer.md` P1 paragraph says "Attachment images stay out of the writer brief data ... KBS-F3 did not change this", while its Method (User message) says "for `attach`, one note per attached image" and `S3-knowledge.md` line 30 says the notes exist "so the writer can point at them in the manifest's reference syntax". Decision 170 item 4 (user accepted) resolves it toward the method text. Confirmed by reading both.
- S3 builds the note as JSON of facet and value pairs (`assembly.py` `_select_images`) and `validate_assembly` checks it; S6 lists `attachments` in pack order (`envelope.py:43`).

## Change

- `src/prompt_enhancer/assembly.py`: new `attached_note(image, facts)`, the former loop body of `validate_assembly` moved verbatim (same checks, same messages) and returning the decoded pairs reduced to `facet` and `value`; `validate_assembly` calls it.
- `src/prompt_enhancer/writer.py`: `render_brief` builds `knowledge.attachments` (only when an `attach` item exists): `position`, `image_id`, `bound_entity`, `facets`, `note` (decoded list of `{facet, value}`), from the same filter and order as `envelope.py:43`. It refuses (`ValueError`) unless `task == 'edit'` and `edit.attach_kb_reference_images is True`. `_data_json` writes one entry per physical line inside the one JSON object, after `images`, only when the list is non-empty. The `attach` literal now comes from `assembly.CHANNEL`.
- No new configuration key. Brief template `prompts/brief_v0.txt`, system message and addendum are untouched.
- Tests: `tests/test_writer_attachments.py` (14 tests), `tests/test_writer.py::test_only_kept_knowledge_is_rendered` no longer feeds an `attach` item (it would now be refused for `generate`; attach coverage lives in the new file).

## Pre-mortem (decision 169): how it could fail, and the test that rules it out

| Failure | Test |
|---|---|
| Brief order differs from the S6 attachments order | `test_entries_follow_s6_order_and_values_stay_data_property`: builds the real `build_envelope(...attachments_ready=True)` and compares ids, order, `bind_to`, `use_for` with the entries; includes interleaved `live_look` items. Mutants M1, M2 killed. |
| A note value renders outside the JSON block or as a sentence (R-026) | same property: each entry equals one `json.dumps` line, the brief parses as one JSON object, values include braces, quotes, newlines, U+2028, template names. `test_entry_carries_position_id_entity_facets_and_decoded_note`. M5 killed. |
| A non-kept value is smuggled (S3 bypassed) | `test_a_note_outside_the_kept_facts_is_never_rendered` (5 cases: value not kept, extra facet, free text, missing value, empty note) and `test_note_pairs_are_reduced_to_facet_and_value`. M4, M9, M10 killed. |
| Retrieval off or the flag false still renders an images entry | `test_retrieval_off_renders_no_knowledge_key`, `test_attach_item_outside_edit_with_attachments_on_is_refused[config0-edit]`. M3 killed. |
| `generate` renders attach notes | `test_attach_item_outside_edit_with_attachments_on_is_refused[config1-generate]` (refused, never rendered). |
| No images selected changes today's bytes | `test_no_images_selected_renders_as_before` (ends with `],"images":[]}}`, no `attachments` key). M6 killed. |
| System message or addendum bytes change | `test_system_addendum_trace_and_repair_keep_their_bytes` (system `b'Admin\r\n'` and addendum equal with and without images). |
| Template substitution runs twice on `{{data}}` or the input variable | the property uses user template `{{aspect_ratio}}\n{{original_prompt}}` and values `{{data}}`, `{{original_prompt}}`, `{{aspect_ratio}}`; a second pass would change the round trip or raise. Existing `test_template_substitution_is_single_pass` passes. |
| S5 repair renders a different brief | same test: `write(..., repair=...)` carries an identical `knowledge` object. |
| Trace stores a brief unlike the sent one | same test: `brief_hash == sha256_tag(sent user text)`; `tests/test_pipeline.py:159` (brief hash of the executed text) passes. |
| Image bytes reach the writer | same test: `images` stays `(('source_image', ...),)` only. |

## Class sweep: readers of `pack['images']`

- `envelope.py:43` (S6 attachments): unaffected; its order is the order the brief copies.
- `assembly.py` `_select_images`, `build_assembly`: unaffected. `validate_assembly`: refactored, same behaviour (45 + 15 + 18 assembly tests green).
- `pipeline.py:70` `require_attachment_copies`, `:81` `retain_attachments`: unaffected (read only `image_id` and `channel`).
- `validation.py` (S5): no direct read; it calls `writer.write` with the same `assembly_result`, so repair renders the same knowledge data (tested).
- `trace` code (`envelope.build_trace_row`, `trace_store.py`): stores `assembled` as before and the writer's `brief_hash`; unchanged. Eval code and `attachments.py` (its `index['images']` is the delivery index, another structure): unaffected.

## Completion report

Behaviour: with `edit` and `edit.attach_kb_reference_images: true`, the writer brief holds `knowledge.attachments`, one line per attached image in S6 order. Every other case renders as before.

Commands (interpreter `<project>/.venv/Scripts/python.exe`, `PYTHONPATH=<worktree>/src:<worktree>/tests`, through `tools/agent-workflow/test_slot.py`, one file per process, `--basetemp .tmp/ATTACH-NOTES/pytest`); log `.tmp/ATTACH-NOTES/run1.log`:

| File | Result |
|---|---|
| tests/test_writer_attachments.py (new) | 14 passed (re-run after the line wrap) |
| tests/test_writer.py | 17 passed |
| tests/test_writer_config.py | 213 passed |
| tests/test_assembly.py / _config / _properties | 45 / 18 / 15 passed |
| tests/test_envelope.py | 7 passed |
| tests/test_pipeline.py, _config, _s0_s3, _s0_s6, _s4_s5 | 18, 62, 1, 14, 1 passed |
| tests/test_retained_images.py | 69 passed, 2 skipped (skips are in the file, not mine) |
| tests/test_validation.py / _config | 44 / 169 passed |
| tests/test_stage_revisions.py | 393 passed |

Intent check per item:
- R-001: the entries are separate data under `knowledge`; `prompt`, `intent` and the constraint block are rendered from the same inputs as before. A note only carries kept fact values.
- R-004: system message and addendum bytes asserted equal; `brief_v0.txt` untouched; the admin template is still substituted in one pass.
- R-006: no field of the note reaches `creativity` or the Intent IR; entries add no permission to invent.
- R-009: retrieval off renders no `knowledge` key at all (test); `attach` needs `rag.enabled` and the flag.
- R-026: values appear only inside JSON strings of one data object, decoded from the S3 JSON note, never a sentence built around them; a note naming a non-kept value is refused.

Changed files: `src/prompt_enhancer/assembly.py`, `src/prompt_enhancer/writer.py`, `tests/test_writer.py`, `tests/test_writer_attachments.py`, this task file.

## Configuration audit

| Row | Value | Class | Evidence |
|---|---|---|---|
| New configuration key | none | n/a | no override or refusal test needed |
| `edit.attach_kb_reference_images` (read) | existing key | configurable | override: `test_entries_follow_s6_order...` (true); refusal: `test_attach_item_outside_edit...[config0-edit]` (false) |
| `edit.max_kb_reference_images`, `assembly.max_images_per_concept` | not read by S4; S3 enforces them | configurable | unchanged |
| `'attachments'`, `'position'`, `'note'` and the other entry keys | brief data keys | protocol-bound (brief contract, mirrors S6 `attachments`) | `test_entry_carries_position_id_entity_facets_and_decoded_note` |
| `CHANNEL = 'attach'` imported from `assembly` | delivery channel | protocol-bound | already classified in assembly.py |
| Position base 1 | brief contract | protocol-bound | same test |
| Hidden defaults, ignored settings, hand-written content rules | none; no word list or regex over natural language; the note is decoded with `json.loads` | n/a | `attached_note` |

## Mutants (10 on the changed lines, scratch overlay, one test file per process)

M1 position starts at 0: killed (property). M2 reversed order: killed (property). M3 refusal removed: killed (config0-edit). M4 note not re-checked: killed (smuggling cases). M5 note left as JSON string: killed (property). M6 `attachments` always emitted: killed (property). M7 facets dropped: killed (property). M8 non-attach images no longer filtered into `images`: killed (property). M9 note keeps extra keys: killed (`test_note_pairs_are_reduced...`). M10 subset check inverted: killed (property). Survivors: none. Scratch removed. Basetemp was 418 KB before removal.

## not_run

- Lint and `ruff format --check`: not_run, ruff not installed; I wrapped my lines to 120 columns by hand (one pre-existing 124-column line at `writer.py:76` is untouched).
- Full suite, `tools/` suite, live model, GPU, network: not_run by the brief.
- No end-to-end pipeline test with a real snapshot and attach image through the writer: the pipeline test files above pass but I did not add one; the writer is covered with a hand-built pack and a real `build_envelope`.

## Noticed, not done

- `prompts/brief_v0.txt` says "any granted knowledge" and does not name `attachments`; the key name is self-describing, but a PM may want a template line telling the writer to point at an image by `position` per the profile's reference syntax. That changes a runtime prompt (and its hash), so I left it.
- The writer never sees the image bytes, so it relies on `facets` and `note` only; if the profile's reference syntax counts the source image as image 1, the mapping from `position` to that syntax is not stated anywhere (S4 doc or code). Worth one decision.
- `writer.py:76` is 124 columns (pre-existing).

## Proposed text for `docs/stages/S4-writer.md`

Replace, in the P1 paragraph, the sentence "Attachment images stay out of the writer brief data (`writer.py` filters the `attach` channel; KBS-F3 did not change this); `kb sync` retains the image copies that S6 delivers ([S6](../../docs/stages/S6-envelope.md))." with:

> For `edit` with `edit.attach_kb_reference_images`, the brief's `knowledge` object carries `attachments`: one entry per `attach` image in exactly the order S6 lists `attachments`, each with its 1-based `position`, `image_id`, `bound_entity`, `facets` and `note`, the note decoded from S3's JSON into facet and value pairs and re-checked against the kept facts the image supports, their entity and the sorted facet list (the rule of `assembly.attached_note`, shared with S3 validation). The entries are delimited data inside the one JSON block, never instructions (R-026); no image bytes reach the writer (`live_look` is unchanged). The key is absent, and the brief is byte-identical to one without attach images, for `generate`, with retrieval off, with attachments off, when no image is selected, and when the active profile's manifest has `renderer.accepts_reference_images: false` (S6 then delivers no image and marks the result `degraded`, so S4 does not describe one; PM decision pending user review, no refusal added). A pack that holds an `attach` image outside `edit` with the flag on is refused. `kb sync` retains the image copies that S6 delivers ([S6](../../docs/stages/S6-envelope.md)). Open question for the user: when the source image is also sent, how does `position` map to the profile's `renderer.image_reference_syntax` (does the source image count as image 1, does the first knowledge image become image 2)? No code for it in this task; copying the syntax verbatim into the brief data is a proposal only.

Proposed text for the writer section of `docs/implementation-notes.md`: "ATTACH-NOTES (decision 170 item 4): `writer.render_brief` renders `knowledge.attachments`, one entry per `attach` image in S6 order; `assembly.attached_note` is the single decoder and checker of an image note, used by S3 validation and S4. With `renderer.accepts_reference_images: false` no `attachments` key is rendered (PM decision pending user review). Left open for the user: how `position` maps to the profile's `renderer.image_reference_syntax` when the source image is also sent; and a `brief_v0` line naming `attachments`."

## Fix round 1 (decision 164, after Opus and Gemini review)

The first-round log `.tmp/ATTACH-NOTES/run1.log` was removed; the final runs are in `.tmp/ATTACH-NOTES/run3.log`.

1. **F1, test gap (R1, R2).** Added in `tests/test_writer_attachments.py`: `test_a_note_naming_a_fact_kept_for_another_entity_is_never_rendered` (fact kept for `e1`, image bound to `e2`, expects `does not support a kept fact`) and `test_facets_out_of_order_are_never_rendered` (two pairs, `facets` reversed, expects `not a kept fact it supports`). Added mutation `image_entity` to `MUTATIONS` in `tests/test_assembly_properties.py` (S3's own validator); the reversed-facets case needs two facets per image, which that fixture does not build, so it lives in the writer file only. Re-run on a scratch overlay after the last edit: **R1 killed** by the writer test and by `test_validate_assembly_refuses_broken_results[image_entity]`; **R2 killed** by `test_facets_out_of_order_are_never_rendered` (`test_assembly_properties.py` still passes R2: S3 only ever writes sorted facets, so the writer-side test is the guard). Intent: tests only, no behaviour change; R-026 gains two guards.
2. **F2 / P3, the property draws what it claims.** `HOSTILE` now holds U+2028, U+2029, U+0085 and a mixed string with `\r\n`, built with `chr()` (my first version held two of them as raw invisible characters and the third as an escape, so my earlier report's "escapes" claim was wrong; corrected here). Each image has one to three facet and value pairs (facts `facet.<image>.<pair>`); the first round used one pair and did not deliberately draw U+2028. What the property asserts about lines: the brief is split on `'\n'` only; the attachments region starts at the line ending `,"attachments":[`, the next lines equal `json.dumps(entry, ensure_ascii=False)` per entry (trailing comma removed), the last line is `]}}`, and no `'\r'` appears. `str.splitlines()` is not used for the check because JSON writes U+2028, U+2029 and U+0085 raw (`ensure_ascii=False`) and `splitlines()` would split inside such a string; `test_line_separator_characters_stay_inside_one_json_line` asserts that (`splitlines` yields more lines than `split('\n')`, while the entry is still one `'\n'` line). The same raw characters already occur in fact lines, unchanged by this task, so a consumer splitting on Unicode line breaks would see the same for facts. Intent: tests only; R-026 (values stay inside one JSON object).
3. **F3, PM decision pending user review.** `render_brief` renders `knowledge.attachments` only when `manifest['renderer']['accepts_reference_images']` is true, the field `pipeline.py:84` (`retain_attachments`) reads. Otherwise the key is absent and the brief equals that of the same pack without attach images (`test_profile_that_accepts_no_reference_images_gets_no_attachments` compares the two strings and checks that `True` renders positions 1 and 2). No refusal added (conservative, reversible); the user decides later whether `attach_kb_reference_images: true` with a profile that accepts none should be refused at configuration time. The refusal of an attach item for `generate` or flag off is checked before this condition and stays. Mutant R3 (condition removed) killed by that test; the inverted condition (R4) killed by 12 property examples (run before the last constant-only edit of the test file). Intent: R-001 and R-006 untouched (only the optional `attachments` key is affected; prompt, IR and constraint block render as before); R-004 (system and addendum untouched, existing byte test); R-009 (retrieval off still renders no `knowledge` key); R-026 (less data, still only JSON data).
4. **Proposed S4 text** above now names item 3 and states the open `position` versus `renderer.image_reference_syntax` question for the user; no code for it.

Commands: the six required files, one per process through `test_slot.py` (`PYTHONPATH=<worktree>/src:<worktree>/tests`, `--basetemp .tmp/ATTACH-NOTES/pytest`): `test_writer_attachments.py` 18 passed (was 14), `test_writer.py` 17 passed, `test_assembly.py` 45 passed, `test_assembly_properties.py` 16 passed (was 15), `test_pipeline_s0_s6.py` 14 passed, `test_retained_images.py` 69 passed, 2 skipped. Basetemp was 7.5 MB before removal; mutation scratch and scripts removed. Lint and `ruff format --check`: not_run (ruff not installed); my lines are within 120 columns, `writer.py:76` (124) is the pre-existing line. Not run: full suite, `tools/` suite, an end-to-end pipeline test with a real snapshot, anything live. Survivors: none of M1 to M10, R1 to R4.
