# Model and data storage

Design only; nothing exists yet.

## Models

Weights live under `models/` in the project root (Git-ignored), uploaded in advance; a missing artifact is acquired only from its pinned source and only when the task permits a download. Each model has a manifest entry verified at load time: repository, revision, filenames, sizes, SHA-256, licence. A quantised model and its projector file have entries with the same fields. Never substitute a model, quantisation or path silently, because a changed file changes every artifact derived from it ([K0-sync](stages/K0-sync.md), R-017).

## Data volume

Working data lives under `data_root` (profile value `storage.data_root`), by default `data/` in the repository (Git-ignored); it may be an absolute path validated for containment. The source knowledge base is read only and only by `kb sync` (R-014): the request path reads `kb_store/` and never the source tree.

```text
<knowledge.root>                source folders and info files, read only, read by kb sync only
<storage.kb_store>              configured separately (example data/kb)
  artifacts/                    content-addressed: embeddings, observations, cards, aliases,
                                optional image copies used by the attach channel
  snapshots/<snapshot_id>/      immutable: Parquet tables, serving index files, manifest
  registry/                     sync_state, snapshots, and REGISTRY naming the authoritative snapshots file
  leases/                       one lease per pinned request, held through an OS lock
  holds/                        snapshots kept while a trace references them
  CURRENT                       pointer to the published snapshot
<storage.data_root>/
  eval/                         eval_scenarios, eval_labels, eval_results
  traces/                       request_traces
```

- **Artifacts** are keyed by the hash of their input plus the revisions of the model, prompt and schema that produced them ([N126](note/N126-content-addressed-caches.md), R-017). Renaming or moving a source file changes no key.
- **Snapshots** are built beside the current one and published by switching `CURRENT`; a request pins its snapshot at S0 and keeps it to the end ([N125](note/N125-snapshots-and-vector-deletion.md)).
- **Serving runtime write access:** the request path writes only coordination files, never knowledge data: `LOCK`, `leases/` and `holds/` under `kb_store` must stay writable by the serving runtime's account, while snapshots, `registry/` and `artifacts/` are written by `kb sync`, `kb gc` and `kb rollback` alone (R-014).
- **Image copies** under `artifacts/` exist only so that the `attach` channel never reads the source tree; whether to keep them is a profile value (`design_required` until the implementing task decides with evidence). The default delivery is text (R-011).
- **Garbage collection:** `kb gc` removes artifacts that no retained snapshot references; a full scan is needed to find orphans ([N124](note/N124-incremental-indexing.md)).

## Why Parquet and Arrow

Tables and embeddings are Parquet or Arrow (R-016): columnar, append-friendly, readable without a server, and identical on Ubuntu and Windows. The serving index uses exact blocked numpy search plus a lexical index over character n-grams (decision 134), derived from those tables and rebuilt per snapshot ([K5-index](stages/K5-index.md)); it is never the source of truth. Base/dev engine and metric remain `design_required`, build settings `fit_from_bench`, and n-gram size `fit_from_gt` until selected or fitted.
