# K4 Aliases: generated offline, collision-checked

**Purpose.** Let a mention in another language, another script or a common spelling reach the right concept, without transliteration tables or per-language rules (R-015, decision 18).

**Input and output.** Concept names and descriptions, the card summary from K3, aliases the admin wrote in `kb_overrides.yaml`, then `aliases` rows: `concept_id`, alias text, origin (`author` for the admin's, `generated` with the model revision), language or script when the model states it, and collision status.

**Method.**
- The `alias_generator` model proposes, per concept, names in other languages, other scripts and common spellings. Readings that no conversion rule produces (for example a Sino-Vietnamese reading of a name) come only from a model or from the admin [A].
- Only Unicode normalisation (NFKC, case folding) is applied structurally; there is no diacritic folding, kana or pinyin conversion and no word list, because those are hand-written language rules.
- **Collision check:** a normalized ownership map includes trusted canonical names and aliases. An alias shared with another concept gets `pending_review` and `collides_with` containing the other concept IDs; it is excluded from automatic matching and queued in collision lint. Canonical names never change status, and an alias equal only to its own name is accepted. Collision ownership is accepted (decision 136).
- **Aliases only add candidates.** A generated alias brings its concept into the S1 shortlist; it never resolves a mention on its own, because resolution is the planner's decision in context ([S2](S2-planner.md)).
- Aliases are placed before dense search in the candidate sources because dense retrievers may degrade on transliterated queries ([N114](../note/N114-transliterated-queries.md), an assumption-class note); whether aliases help on the user's data is measured in E4.

**Decisions and configuration keys.** `aliases.*`: the prompt revision, the languages and scripts to generate for (`design_required` from the knowledge base and the expected users), a call budget for paid backends (`ask_user`); `vlm.roles.alias_generator`. Cache key: concept name and description hashes, the hash of the card summary, plus generator and prompt revisions ([N126](../note/N126-content-addressed-caches.md)).

**Configuration.** `aliases.languages` is an explicit nonempty list of language/script labels; the base example keeps `design_required`, while dev selects `[vi, en, ja, zh]` (decision 135). The list is prompt data, without per-language code. K4 refuses missing/sentinel read values before any writer/client. Supported policy is `drop_and_queue_for_review`, with `review_queue` and `only_add_candidates` explicitly true; unsupported values refuse. `generated: false` produces only author rows, without creating a model client. Provider budgets, concurrency, timeouts and inference settings come from the existing role/backend configuration, without new K4 defaults.

**Compute.** `src/prompt_enhancer/k4_aliases.py` implements unpublished K4. Text-only generation uses the configured role and ID-free `alias_generator_reply.v0`; code constructs stored rows. Checkpoints include concept name/assigned-description/card-summary hashes, languages, semantic model/inference settings, prompt bytes and schemas. Author edits and collision ownership are reapplied to cached candidates without generation. For one concept/exact raw alias, author wins and has null model/prompt provenance; the generated candidate remains in its checkpoint. Distinct spellings stay distinct rows. Membership/card inputs are indexed once; collisions group by normalized text, with work proportional to input plus emitted collision references (and deterministic ordering). No live alias quality, model resistance or performance has been measured. K5, full-sync wiring and publication are not implemented here.

**A failed alias call does not block the publish (decision 165).** In `kb sync`, a concept whose generation fails (for example a reply that repeats to `max_tokens`) gets no generated rows in that snapshot, so its `concept_texts` embed no generated alias; its author aliases from `kb_overrides.yaml` stay, because they need no model call and `_alias_rows` merges them independently of the model reply. The failure is recorded as concept ids and closed codes only (`kb sync` result `alias_failures`; `k4_failed` in the snapshot `source_scan_summary`), and the next sync re-calls only the failed keys. `build_aliases` itself is unchanged: it still returns the failed map in `report['failed']`; the policy lives in `kb_sync.sync_knowledge`, and every other stage still blocks on failure. Only per-concept codes are tolerated (`SCHEMA_INVALID`, `INCOMPLETE_OR_REFUSED`, `MODEL_CALL_INVALID`, `MODEL_CALL_FAILED`, `HTTP_ERROR`, `INPUT_INVALID`; `kb_sync._PER_CONCEPT_CODES`); a run-level code (configuration, client state, capability, probe or budget) means no concept could succeed, so it still fails `k4: STAGE_FAILED` and nothing is published (PM decision pending user review, 2026-11-19).

**Depends on.** K0 and K3 (the card summary).

**Failure modes.** A plausible but wrong alias pulls a wrong concept into the shortlist (the planner can still reject it; measured in E1); aliases in a language the model knows poorly; many collisions in a large knowledge base (review queue grows).

**Experiments.** E1 (linking per language and script), E4 (with and without aliases).

**References.** N114, N126, [N132](../note/N132-multilingual-retrieval-evidence.md).
