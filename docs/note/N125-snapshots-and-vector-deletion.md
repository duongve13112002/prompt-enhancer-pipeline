# N125 Reader snapshots and deleting vectors

- **Source:** https://www.sqlite.org/wal.html ; https://docs.lancedb.com/tables/versioning ; https://github.com/facebookresearch/faiss/wiki/Special-operations-on-indexes
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** K0, K5

## What this project relies on

A write-ahead-log database gives each reader a fixed snapshot with one writer at a time. A versioned table format commits a new version on every write and lets a reader pin a version. FAISS supports `remove_ids` for IndexFlat, IndexIVFFlat and IDMap; HNSW is not listed.

## Caveats

Agent-read. This project rebuilds the serving index per snapshot instead of deleting in place.
