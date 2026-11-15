# Decisions 13 to 17 (archived verbatim from tasks/DECISIONS_INDEX.md on 2026-11-15)

Moved to keep the index within its character budget; the rows are unchanged and still in force unless a later decision replaces them.

| ID | Date | Decision | Source | Applied |
| --- | --- | --- | --- | --- |
| 13 | 2026-11-11 | Evidence comes from concept cards built offline; reading knowledge-base images during a request is only a fallback | user answer ("option B") | applied: docs/stages/K2-observe.md, docs/stages/K3-cards.md |
| 14 | 2026-11-11 | Knowledge goes downstream as text by default; generate is always text; edit attaches knowledge-base images only when the configuration turns it on | user answer | applied: docs/stages/S3-knowledge.md, configs/enhancer.example.yaml |
| 15 | 2026-11-11 | A mention broader than the knowledge base: choose by context, else at random; a new choice for every request, recorded in the trace | user answers | applied: docs/stages/S2-planner.md |
| 16 | 2026-11-11 | Variants inside one concept follow the same rule as decision 15 | user answer | applied: docs/stages/S3-knowledge.md, docs/stages/K3-cards.md |
| 17 | 2026-11-11 | The knowledge base is managed: add, change and delete reprocess only what is affected; deleting or moving an original never breaks the database or a running request; a deleted source loses its knowledge at the next sync | user answers | applied: docs/stages/K0-sync.md, docs/execution.md |
