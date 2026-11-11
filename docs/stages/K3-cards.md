# K3 Cards: one card per concept

**Purpose.** Turn per-image observations into one concept card per concept that says, facet by facet, what is intrinsic to the concept, what varies and what the evidence cannot decide, so S3 can hand the writer facts with known reliability (R-010, decision 13).

**Input and output.** `observations`, `folder_concepts`, the descriptions from the info files and `kb_overrides.yaml`, then `cards` (a short card summary text per concept, used by K4 and by the concept-text embeddings of K1 part 2; per concept and facet: observed values, supporting images over images where the facet is visible, a stability label, variant groups with `image_id`s, origin) and a lint report for the admin. Schema: [contracts](../../contracts/README.md).

**Method.**
- **Per concept across all folders (decision 10).** A card aggregates every folder that declares the concept: folders `kimono` and `kimono_donut` (its info file declares `kimono | donut`) both feed the card of `kimono`. An attribute that holds across both folders has more support than one seen in a single folder.
- **Equivalent values** ("long black hair", "black hair, long") are grouped by the `card_builder` model, because deciding that two phrasings mean the same needs meaning, not string rules (R-015).
- **Stability labels [P]**, by the criterion of **invariance under variation**: an attribute that stays the same while background, outfit and view change is evidence of something intrinsic; without variation the card says it cannot tell instead of guessing.

| Label | Condition | Use online ([S3](S3-knowledge.md)) |
| --- | --- | --- |
| `curated` | from an info file or `kb_overrides.yaml` | a checked fact, within its wording |
| `stable` | enough supporting images (`cards.min_visible_images`, `fit_from_gt`), no conflicting value, and variation on other facets | a trait of the concept |
| `stable_low_diversity` | no conflict, but the images are near-identical | only for facets the role treats as intrinsic |
| `varies` | two or more values | variant groups; the choice follows decision 16 in S2 and S3 |
| `single_observation` | seen in one image only | not used as a fact by default |

- **Never a majority vote.** Many similar images do not make a value canonical, and an accessory not seen does not prove it absent; `varies` keeps every group with its images instead.
- **Recomputed, never accumulated:** a card is rebuilt from the current image list at each sync ([N124](../note/N124-incremental-indexing.md)).
- **Lint report:** a description that contradicts the images; a facet the role needs that no image shows clearly; low diversity (a folder of near-duplicates). The report goes to the admin through `kb show` and `kb status`.
- **Origin.** Every model-generated field carries `origin: generated` and the model revision; the user's names and descriptions are never overwritten. `kb_overrides.yaml` can set a concept type, add aliases, assert facts (treated as `curated`) and suppress facets, without touching images.

**Decisions and configuration keys.** `cards.*`: `cards.min_visible_images` and the low-diversity value (`fit_from_gt`, E16), stability policy version; `vlm.roles.card_builder`; `knowledge.overrides_file`. Cache key: the observation keys, the info-file and override hashes, the card builder revision, the stability policy version ([N126](../note/N126-content-addressed-caches.md)).

**Compute.** CPU over Parquet plus calls to the `card_builder` backend for value grouping and lint. Not implemented; nothing measured.

**Depends on.** K2.

**Failure modes.** A wrong card is wrong for every request (the E16 audit gates card use in production); value grouping merges two different values; a concept whose folders show different subjects under one name (reported in lint).

**Experiments.** E16 (card and label precision), E9 (leakage counterfactual), E14.

**References.** N124, N126, [N109](../note/N109-evisrag.md).
