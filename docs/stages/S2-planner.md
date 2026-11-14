# S2 Planner: parse, resolve, plan in one call

**Purpose.** Understand the request once: what the user asked for, which knowledge-base concepts the mentions refer to and in what role, what knowledge is still missing, and which constraints the output must keep (R-001, R-002, R-008).

**Input and output.** The raw prompt, the source image for `edit`, the S1 shortlist as delimited data, the admin profile's manifest (supported tasks, quote semantics) and the creativity configuration, then the Intent IR as JSON against a schema ([contracts](../../contracts/README.md)): `task` and `edit_kind`; entities, attributes and relations, each value marked as coming from the user; concept links; knowledge needs (per resolved concept, the facets still open after removing what the user fixed); the constraint ledger ([S5](S5-validate.md)); literal texts and passthrough tokens kept character for character; unresolved parts. With retrieval off there is no shortlist and the links are empty.

**Method.**
- **One call does parsing, concept resolution and knowledge planning,** because they share context: whether a dress is the character's garment or a separate concept, or whether a mention is negated, is known only once the sentence is understood.
- **Concept links** take one status per mention: `resolved` (with `concept_id` and role), `family` (the mention is broader than the knowledge base) or `no_match`. Choosing from a shortlist with an explicit no-match option follows entity-linking practice ([N115](../note/N115-llm-entity-linking-nil.md)).
- **Role per request, not from the concept kind (R-008):** `subject_identity`, `worn_garment`, `location`, `object`, `style_reference` or `other`. A character concept can serve as a `style_reference`.
- **Family by context, not by name (decision 31).** No name structure makes folders a family; the planner decides from the shortlist and the prompt's context whether several concepts fit one mention, and code then chooses as decision 15 says.
- **Context never vetoes the user (decision 69).** A concept or variant the user names is used even when the scene seems to contradict it (a summer uniform in snow is a valid request); context only chooses where the user left the choice open.
- **Family choice (decision 15, R-013).** The model rates each candidate against the prompt's context as `fits`, `neutral` or `conflicts`. Code keeps `fits` if any, else `neutral`, else all candidates (decision 69: a rating never empties the set), then picks uniformly at random with a fresh seed per request; the seed, ratings and pick go to the trace. The pick is done by code so that it is uniform and traceable. `resolution.on_family_match` selects `context_then_random` (default, decision 15), `omit` (the mention is treated as `no_match` and no fact is added) **[P]** or `ask` (the response has status `needs_clarification` and lists the candidates) **[P]**. `ask` is valid only with `clarification.mode: ask` (decision 68). Variants inside one concept follow the same procedure (decision 16, [S3](S3-knowledge.md)).
- **Second-chance lookup:** when a mention needs knowledge but no shortlist entry fits, one extra lookup runs for that mention's text and one small resolution follows; bounded to one round.
- **Edit outputs:** `edit_kind` (`local_edit`, `global_transform`, `subject_recompose`, which set default preservation and creativity, R-002), targets (short locating descriptions), delta (the operations), preserve (what the user asked to keep plus the kind's defaults, named by type, place and role rather than described, [N100](../note/N100-qwen-image-pe-i2i.md)), dependent effects (what a removal or move exposes), and, for an ambiguous target, the candidate targets: with `clarification.mode: ask` the response is `needs_clarification`; with `best_effort` (the example value, decision 68) the most plausible target is used, the status is `degraded` and the reason goes to the trace. Retrieval for edit uses the target described in text ([N119](../note/N119-composed-image-retrieval.md)); linking entities in the source image to the knowledge base is off by default (`edit.source_entity_linking`).
- **Thinking and structured output:** strict formats can reduce reasoning quality ([N117](../note/N117-format-restrictions-and-reasoning.md)), so where the backend allows, the model may think before emitting schema-constrained JSON; whether thinking is on online is decided by E10. The IR uses short keys and never repeats the prompt, because generated tokens set latency.

**Decisions and configuration keys.** `planner.*` (prompt revision, thinking, sampling, token limits: `fit_from_bench` and E10, E11), `resolution.on_family_match`, `clarification.mode`, `creativity.*`, `edit.source_entity_linking`, `vlm.roles.planner`.

**Compute.** One call to the `planner` backend; text only for `generate`, with the source image for `edit`. Not implemented; nothing measured.

**Depends on.** S0, S1.

**Failure modes.** Output that fails the schema is retried once, then the request takes the path without knowledge with status `degraded`; a wrong resolution (measured in E1); a missed negation (caught by the ledger in S5); an unreadable source image is an error.

**Experiments.** E1, E3 (planner versus a reranker before resolution), E6 (planner and writer merged on the path without knowledge), E10, E11, E12.

**References.** N100, N115, N117, N119, [N120](../note/N120-long-lists-in-context.md).
