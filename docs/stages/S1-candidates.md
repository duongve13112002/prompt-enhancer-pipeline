# S1 Candidates: shortlist without a model call

**Purpose.** Find the few concepts the prompt may refer to, cheaply and deterministically, so the planner chooses from a short list instead of the whole catalogue. Runs only with retrieval on; with `rag.enabled: false` it is not in the graph (R-009).

**Input and output.** The raw prompt and the pinned snapshot's index ([K5](K5-index.md)), then a shortlist: per entry `concept_id`, name, kind if known, a one-line description, the source that matched (name, alias, lexical, dense) and the prompt span it matched. An empty shortlist is a valid result.

**Method.**
1. **Name and alias lookup** over the prompt after Unicode normalisation (NFKC, case folding): exact matches of names and aliases ([K4](K4-aliases.md)).
2. **Lexical search** over character n-grams of names, descriptions and aliases, which needs no word segmentation for any language.
3. **Dense search** over the whole prompt and over its segments (split by structure: commas, lines, JSON values), because users name concepts both exactly and indirectly ("a traditional Vietnamese dress"); dense search is a main source, not only a safety net.
4. **Fusion by rank** (reciprocal rank fusion, unweighted) until labelled pairs exist; then a convex combination fitted on them, which can outperform rank fusion with few labelled examples ([N113](../note/N113-fusion-functions.md)).

The whole catalogue is never placed in the planner context, because accuracy falls as lists grow and distractors look alike ([N120](../note/N120-long-lists-in-context.md), [N078](../note/N078-lost-in-the-middle.md)).

**Decisions and configuration keys.** `candidates.*`: shortlist size and per-source depth (`fit_from_gt`, E1), enabled sources (E4), fusion method and weight (`fit_from_gt`), optional reranker (off by default, E3); `embedding.*` for the query encoder, which must match the snapshot's.

**Compute.** CPU lookups plus embedding a few short texts; no model generation. Not implemented; nothing measured.

**Depends on.** S0, K5.

**Failure modes.** The right concept is missing from the shortlist (the planner can ask for a second-chance lookup, [S2](S2-planner.md)); a negated mention matches ("no conical hat" pulls in the hat concept), which the planner rejects in context; an index error (retrieval state `failed`, the request continues without knowledge).

**Experiments.** E1 (shortlist recall per language and script), E3 (reranker), E4 (each source on and off), E5 (embedder).

**References.** N078, N113, N114, N120, [N132](../note/N132-multilingual-retrieval-evidence.md).
