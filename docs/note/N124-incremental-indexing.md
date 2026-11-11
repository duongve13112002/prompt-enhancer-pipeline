# N124 Incremental indexing and orphan cleanup

- **Source:** https://reference.langchain.com/python/langchain-core/indexing/api/index ; https://docs.getdbt.com/docs/build/incremental-models-overview
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** K0, docs/execution.md

## What this project relies on

One indexing API's `incremental` cleanup does not remove a source deleted from disk, while its `full` cleanup removes everything not returned in the run and therefore needs the whole source set every time. An incremental table model does not remove rows that disappeared from the source unless logic is added.

## Caveats

Used as design evidence for a full scan with a mass-deletion guard and for recomputing aggregates from current membership.
