# K4 Aliases: generated offline, collision-checked

**Purpose.** Let a mention in another language, another script or a common spelling reach the right concept, without transliteration tables or per-language rules (R-015, decision 18).

**Input and output.** Concept names and descriptions, the card summary from K3, aliases the admin wrote in `kb_overrides.yaml`, then `aliases` rows: `concept_id`, alias text, origin (`author` for the admin's, `generated` with the model revision), language or script when the model states it, and collision status.

**Method.**
- The `alias_generator` model proposes, per concept, names in other languages, other scripts and common spellings. Readings that no conversion rule produces (for example a Sino-Vietnamese reading of a name) come only from a model or from the admin [A].
- Only Unicode normalisation (NFKC, case folding) is applied structurally; there is no diacritic folding, kana or pinyin conversion and no word list, because those are hand-written language rules.
- **Collision check:** an alias that points to two concepts is dropped from automatic use and queued for review in the lint report.
- **Aliases only add candidates.** A generated alias brings its concept into the S1 shortlist; it never resolves a mention on its own, because resolution is the planner's decision in context ([S2](S2-planner.md)).
- Aliases are placed before dense search in the candidate sources because dense retrievers may degrade on transliterated queries ([N114](../note/N114-transliterated-queries.md), an assumption-class note); whether aliases help on the user's data is measured in E4.

**Decisions and configuration keys.** `aliases.*`: the prompt revision, the languages and scripts to generate for (`design_required` from the knowledge base and the expected users), a call budget for paid backends (`ask_user`); `vlm.roles.alias_generator`. Cache key: concept name and description hashes, the hash of the card summary, plus generator and prompt revisions ([N126](../note/N126-content-addressed-caches.md)).

**Compute.** Text-only calls to the `alias_generator` backend; rerun only for concepts whose name, description or card summary changed. Not implemented; nothing measured.

**Depends on.** K0 and K3 (the card summary).

**Failure modes.** A plausible but wrong alias pulls a wrong concept into the shortlist (the planner can still reject it; measured in E1); aliases in a language the model knows poorly; many collisions in a large knowledge base (review queue grows).

**Experiments.** E1 (linking per language and script), E4 (with and without aliases).

**References.** N114, N126, [N132](../note/N132-multilingual-retrieval-evidence.md).
