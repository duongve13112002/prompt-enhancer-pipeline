# K5 Index: derived serving index per snapshot

**Purpose.** Build, for each snapshot, the read-only index S1 and S3 query during a request (R-016, decision 19). The index is derived: it can always be rebuilt from the snapshot's tables.

**Input and output.** `concepts`, `aliases`, `cards`, `concept_text_embeddings`, `image_embeddings` of the new snapshot, then inside the snapshot directory: an approximate nearest-neighbour index over concept-text vectors, one over image vectors, a lexical index over character n-grams of names, descriptions and aliases, and an exact lookup of normalised names and aliases.

**Method.**
- **Character n-grams** need no word segmentation, so one lexical index serves Vietnamese, Japanese, Chinese and mixed prompts without a per-language tokenizer [A]: no comparison of word segmentation against character n-grams for these languages was found.
- **Rebuilt, not edited.** Each sync builds a fresh index for the new snapshot, because some approximate nearest-neighbour structures cannot delete entries ([N125](../note/N125-snapshots-and-vector-deletion.md)); no all-pairs comparison is made (R-016).
- **Validation before publish:** every index entry points to a row of the snapshot, and sample queries (each concept's own name) return the concept.

**Decisions and configuration keys.** `index.*`: engine and index type (`design_required`, with the options in N047 and N048), build parameters (`fit_from_bench`), n-gram length (`fit_from_gt`, E1).

**Compute.** CPU, or GPU for large image-vector sets; build time grows with the knowledge base and is measured on the deployment. Not implemented; nothing measured.

**Depends on.** K1, K3, K4.

**Failure modes.** An index built from a different embedder revision than the query encoder (prevented by keying); a large image index that does not fit memory (`fit_from_bench`).

**Experiments.** E1, E4, E13 (lookup latency under concurrency).

**References.** [N047](../note/N047-lance.md), [N048](../note/N048-faiss-gpu.md), N125.
