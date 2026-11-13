# Model and data storage

Design only; nothing exists yet.

## Models

Weights live under `models/` in the project root (Git-ignored), uploaded in advance; a missing artifact is acquired only from its pinned source and only when the task permits a download. Each model has a manifest entry verified at load time: repository, revision, filenames, sizes, SHA-256, licence. A quantised model and its projector file have entries with the same fields. Never substitute a model, quantisation or path silently, because a changed file changes every artifact derived from it ([K0-sync](stages/K0-sync.md), R-017).

## Data volume

Working data lives under `data_root` (profile value `storage.data_root`), by default `data/` in the repository (Git-ignored); it may be an absolute path validated for containment. The source knowledge base is read only and only by `kb sync` (R-014): the request path reads `kb_store/` and never the source tree.

```text
<knowledge.root>                source folders and info files, read only, read by kb sync only
data_root/
  kb_store/
    artifacts/                  content-addressed: embeddings, observations, cards, aliases,
                                optional image copies used by the attach channel
    snapshots/<snapshot_id>/    immutable: Parquet tables, serving index files, manifest
    CURRENT                     pointer to the published snapshot
  eval/                         eval_scenarios, eval_labels, eval_results
  registry/                     sync state, runs, model manifests
  traces/                       request_traces
```

- **Artifacts** are keyed by the hash of their input plus the revisions of the model, prompt and schema that produced them ([N126](note/N126-content-addressed-caches.md), R-017). Renaming or moving a source file changes no key.
- **Snapshots** are built beside the current one and published by switching `CURRENT`; a request pins its snapshot at S0 and keeps it to the end ([N125](note/N125-snapshots-and-vector-deletion.md)).
- **Image copies** under `artifacts/` exist only so that the `attach` channel never reads the source tree; whether to keep them is a profile value (`design_required` until the implementing task decides with evidence). The default delivery is text (R-011).
- **Garbage collection:** `kb gc` removes artifacts that no retained snapshot references; a full scan is needed to find orphans ([N124](note/N124-incremental-indexing.md)).

## Why Parquet and Arrow

Tables and embeddings are Parquet or Arrow (R-016): columnar, append-friendly, readable without a server, and identical on Ubuntu and Windows. The serving index (approximate nearest neighbour plus a lexical index over character n-grams) is derived from those tables and rebuilt per snapshot ([K5-index](stages/K5-index.md)); it is never the source of truth.
