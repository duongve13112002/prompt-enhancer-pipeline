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

**Library contract (T-013 P2).** `build_candidates(IntakeResult, embedder=...)` uses S0's single pin and effective configuration. `candidate_shortlist.v0` carries stage-local ready/failed state, candidates, structural warnings and an optional structural error; final retrieval state remains the coordinator's responsibility. Calling S1 with retrieval off is a programming error. No index or encoder is constructed on that path. A successful empty shortlist is valid. The supplied K1-compatible query encoder stays caller-owned.

**Configuration.** Required per-source `depth` and `rrf_constant` retain `fit_from_gt`; `max_prompt_windows` retains `fit_from_bench` in base/dev. Only `fusion: rrf` is implemented: sum reciprocal `(constant + one-based rank)` across three unique-concept source lists, stable concept-ID ties. Repeated queries/embedding rows do not add votes. Dense depth bounds rows per query and modality and final source concepts; duplicate rows can therefore yield fewer unique concepts. Query settings reuse K1's space identity but replace the document template with `embedding.query_text_template` (`design_required` until E5). `query_text_limit` is a positive rendered-code-point bound; an overlong dense query is skipped with `DENSE_QUERY_TOO_LONG`, never truncated.

**Bounds and failures.** Exact span width derives from the longest normalized pinned name/accepted alias and Unicode's maximum decomposition expansion, allowing normalization to shorten spans. The work budget counts admitted exact probes plus enabled lexical/dense query units; exceeding it returns failed S1 with `WINDOW_BUDGET_EXCEEDED`, allowing the request to continue without knowledge. Missing snapshots and operational index/embedding failures also return failed S1. Invalid configuration and incompatible query spaces raise before calls. No thresholds or production performance have been fitted.

**Compute.** CPU lookups plus embedding short texts through K1's interface; no model generation. P2 tests use labelled doubles only; no live model or speed measurement.

**Depends on.** S0, K5.

**Failure modes.** The right concept is missing from the shortlist (the planner can ask for a second-chance lookup, [S2](S2-planner.md)); a negated mention matches ("no conical hat" pulls in the hat concept), which the planner rejects in context; an index error (retrieval state `failed`, the request continues without knowledge).

**Experiments.** E1 (shortlist recall per language and script), E3 (reranker), E4 (each source on and off), E5 (embedder).

**References.** N078, N113, N114, N120, [N132](../note/N132-multilingual-retrieval-evidence.md).
