# S3 Knowledge: filter, select, deliver

**Purpose.** Turn resolved concepts and knowledge needs into a Knowledge Pack the writer may use in full, so that nothing out of scope reaches it (R-012). Runs only with retrieval on and makes no model call except the on-demand question below.

**Input and output.** The Intent IR (links, roles, needs, locks, negations), the cards and observations of the pinned snapshot, the configuration, then the **Knowledge Pack**: facts with ID, concept, facet, source and stability label, each bound to the entity that receives it; images per delivery channel with role `knowledge_reference`, receiving entity and facets to use; and every fact left out with its reason (`OUT_OF_SCOPE`, `USER_LOCKED`, `NEGATED_BY_USER`, `UNSTABLE`, `VARIANT_NOT_CHOSEN`, `BUDGET`).

**Method.**
1. **Scope filter.** The role by facet matrix is configuration (`assembly.scope_matrix`) [P], validated by E9. Filtering before the writer is what prevents leakage, because retrieval-augmented generators over-rely on references ([N110](../note/N110-kitten.md)). Starting proposal:

| Role | Facets allowed | Never |
| --- | --- | --- |
| `subject_identity` | `identity` | `incidental`, `text_in_image` |
| `worn_garment` | `apparel` structure, pattern, material, accessories; colour unless the user fixed it | the wearer's `identity`, `incidental` |
| `location` | `place` | crowd, weather, time, camera |
| `object` | `object` | background, who holds it |
| `style_reference` | `style` | the reference image's subject |

   A character's default outfit follows `assembly.default_outfit` (`omit` by default).
2. **Subtract what the user fixed:** a user-given colour removes the card's colour; a negation ("no hat") removes every fact about the negated item.
3. **Stability policy** by label ([K3](K3-cards.md)): `curated` and `stable` are used; `stable_low_diversity` only for facets the role treats as intrinsic; `single_observation` is left out; `varies` follows decision 16: the variant the user named, else the planner's context ratings and a uniform random pick by code, recorded in the trace.
4. **Image selection as greedy coverage** of the needed facets over cached observations: prefer `clear` visibility and quality, at most one image per near-duplicate group; coverage is the goal, not diversity. Prefer an image from a folder that declares several requested concepts, since one image then covers several needs; for `attach` or `live_look` prefer single-concept folders or the stored region, so an accompanying concept does not ride along.
5. **Delivery channel (decision 14, R-011):**

| Task | Configuration | Sent downstream |
| --- | --- | --- |
| `generate` | any | the enhanced prompt only; knowledge as text |
| `edit` | `edit.attach_kb_reference_images: false` (default) | the user's image and the enhanced prompt; knowledge as text |
| `edit` | `edit.attach_kb_reference_images: true` | the user's image, the enhanced prompt and at most `edit.max_kb_reference_images` selected images |

   Internally `verbalize` (facts as text, always with retrieval on), `attach` (images in the envelope, with per-image notes from observations so the writer can point at them in the manifest's reference syntax), `live_look` (the writer reads knowledge-base images; only when `rag.evidence_mode` is `live` or `cards_then_live` and cards miss a needed facet). `live_look` is internal: the writer model reads the selected knowledge images and nothing extra goes to the downstream image model, so it is compatible with R-011 ("generate is text") and is available for both tasks whenever `rag.evidence_mode` allows live reading.
6. **On-demand question:** a facet outside the schema (the pattern on a sleeve) is asked of one image by the `observer` role, where the evidence mode permits, and cached by image hash and normalised question.

**Decisions and configuration keys.** `assembly.scope_matrix`, `assembly.default_outfit`, `assembly.max_images_per_concept` (`design_required`), `rag.evidence_mode` (`cards` by default, decision 13), `edit.attach_kb_reference_images`, `edit.max_kb_reference_images`.

**Compute.** CPU over cached tables. Not implemented; nothing measured.

**Depends on.** S2, K3, K5.

**Failure modes.** A scope matrix that admits an incidental facet (E9 detects it); no usable evidence (state `no_match` and an empty pack, recorded; `skipped_no_knowledge_needed` means only that the planner listed no knowledge need, see [contracts](../../contracts/README.md#enums)); `text_in_image` entering the brief, allowed only when the role needs it and the user did not forbid text, then as quoted literal data ([N131](../note/N131-image-prompt-injection.md)).

**Experiments.** E2, E8, E9, E14.

**References.** N110, [N111](../note/N111-retrieval-augmented-generation.md), N131, [N100](../note/N100-qwen-image-pe-i2i.md).
