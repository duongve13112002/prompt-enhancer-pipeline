# T-003 Evaluation set from the knowledge base

Kind: docs
R-IDs: R-023, R-003, R-013, R-015, R-016
Task file: tasks/active/T-003-evaluation-set.md

## Brief header

- **Goal:** a first evaluation sample built from the user's knowledge base (`database/`, read only), with answer keys taken from the folder declarations, for the user to review (R-023, [G-evaluation](../../docs/stages/G-evaluation.md)).
- **Done check:** `.venv/Scripts/python.exe -B -I .tmp/T-003/build_sample.py --manual data/eval/draft-2026-11-14/scenarios.manual.json --kb-root database --schemas contracts/schemas --out-dir data/eval/draft-2026-11-14 --separator "|" --info-names infor.txt info.txt --split-seed 20261114` prints `ok`; a model of another family has checked the keys; the user has reviewed `sample-for-review.md`; the hash list of `database/` is unchanged.
- **Scope:** `.tmp/T-003/`, `data/eval/draft-2026-11-14/` (Git-ignored), this file. **Out of scope:** generator code under `src/` or `tools/` (needs `BẮT ĐẦU IMPLEMENTATION T-003`, decision 58), any write under `database/`, fitting any value.
- **Ignored evidence paths:** `.tmp/T-003/kb-inventory.md`, `.tmp/T-003/database-hashes.before.txt`, `data/eval/draft-2026-11-14/`.

## Step 0 (approved by the user on 2026-11-14, decisions 54 to 58)

- Scope now: inventory, scenario design and a silver sample built directly from the info files, before any snapshot exists; Parquet under `data_root`; `pyarrow` in `.venv` ("Snapshot là sao nhỉ còn lại ok nha").
- About 5 scenarios per kind, written by hand by the orchestrator; another model family checks the keys ("nếu dc bạn có thể tự manual thì hay nha"; "PM tự viết tay").
- No fixed language and no fixed format ("Không cố định ngôn ngữ hay formta gì nha").
- Agents may open knowledge-base images for this task ("Tui nghĩ là cái này thì thoải mái nha").
- Generator code waits for the implementation command ("Ok nha").

## Phase 1: inventory (done by the PM, read-only)

Command: `.venv/Scripts/python.exe -B -I .tmp/T-003/kb_inventory.py --root database ...`; output `folders=43 files=1417 concepts=47 mismatch=9 no_info=0`. Report: `.tmp/T-003/kb-inventory.md`.

- 43 folders, each with `info.txt`; 47 concept ids; 9 concepts are declared by two or three folders (for example `Áo bà ba` by 3).
- 9 folders declare several names on line 1 and one unsplit paragraph on line 2 (now covered by decision 59).
- 438 JSON files next to images in 4 folders (ignored, decision 60).
- `Luc_quan_Mùa_hè` and `Luc_quan_xuan_he` declare one concept under two spellings (decision 62: the user edits one info file).

## Phase 2: how each key comes from the declarations

| Kind | Key |
| --- | --- |
| single_concept, multi_concept | each mention resolves to the concept whose declared name the scenario was built from |
| family_mention | status `family` with the declared concepts the general mention fits; members whose declared description contradicts the prompt's context are listed as conflicts |
| variant_unspecified | the concept, plus a note quoting the variants its declared description names |
| no_match | status `no_match` for a name no info file declares |
| negation | the concept with `NEGATED_BY_USER`, and a `negation` constraint with the verbatim span |
| literal_text | a `literal_text` constraint whose span is the exact text |
| edit_local, edit_global, edit_recompose | targets, delta and preserve list stated by the scenario; the source image is a knowledge-base image the author looked at |
| rag_off | no concept link at all; constraints only |

## Phase 3: sample (written by hand, checked by script)

- `data/eval/draft-2026-11-14/scenarios.manual.json`: 55 scenarios, 11 kinds, 5 each; Vietnamese, English, Japanese, Chinese and mixed prompts; prose, tags and JSON.
- Check output on 2026-11-14: `ok scenarios=55 ... splits={'fit': 33, 'test': 22} concepts_used=35 of 47`. The script refuses a concept name that no info file declares, a mention or constraint span that is not a verbatim part of the prompt, a missing source image, a schema error in the v0 projection, and a concept in both splits.
- Outputs: `scenarios.parquet`, `scenarios.v0.jsonl`, `sample-for-review.md`.
- Label: silver, `generator.model_revision` names the orchestrator session. Five source images were viewed by the author (decision 57).

## Open

- Key check by another family (Gemini 3.8 Flash `medium`, read-only) once Antigravity has quota: `not_run`.
- The user accepted the sample on 2026-11-14 and asked for harder scenarios where useful (decision 65). Candidates for the next round: indirect mentions built only from a description, names without diacritics or romanised, typos, mentions in a script other than the name's, several negations in one prompt, long nested JSON prompts, prompts whose context makes every family member conflict, and edits that touch a concept the user also locks.
- After decision 64 (folder `Luc_quan_Mùa_hè` deleted by the user) the sample was re-keyed: family-01 now lists one spring-summer member; check output `ok scenarios=55 ... concepts_used=34 of 46`; inventory `folders=42 files=1311 concepts=46 mismatch=9 no_info=0`.

## Noticed, not done

- `eval_scenario.v0` has no field for family members, a negated concept, the edit targets, delta and preserve list, language, format or the retrieval switch, although [G-evaluation](../../docs/stages/G-evaluation.md) names most of them. The draft keeps them in `draft_*` columns; a schema v1 is a contract change for the task that implements `eval build`.
- `docs/execution.md` says `eval build` needs a published snapshot; this draft was built from the info files instead and must be rebuilt or re-keyed once a snapshot exists.
- The split seed `20261114` is an argument of the draft, not a fitted value; `evaluation` has no split-seed key in the example profile yet.
