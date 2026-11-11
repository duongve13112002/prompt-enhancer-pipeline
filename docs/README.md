# Documentation index

Nothing is implemented or measured yet; statuses describe the documents.

| Document | Content | Status |
| --- | --- | --- |
| [architecture.md](architecture.md) | Principles, flow, outcomes, paths and call counts | proposal, not yet implemented |
| [execution.md](execution.md) | Run modes, resume, sync hand-off, snapshots | design |
| [serving.md](serving.md) | Deployment model facts, latency design, run guide | design, nothing measured |
| [dev-machine.md](dev-machine.md) | Small-VRAM dev machine, test model, capability probe, VRAM before and after | smoke only |
| [model-storage.md](model-storage.md) | Where models, `kb_store`, snapshots and traces live | proposal |
| [dependencies-and-environments.md](dependencies-and-environments.md) | Environment profiles and version facts | facts recorded, no lockfile |
| [test-plan.md](test-plan.md) | Test categories and where they run | plan |
| [ci.md](ci.md) | GitHub Actions design and the visibility window procedure | design, research required |
| [implementation-notes.md](implementation-notes.md) | Decisions, deviations, assumptions, risks | maintained |
| [rules/PLATFORM_GPU_RULES.md](rules/PLATFORM_GPU_RULES.md) | Binding detail on platform, builds, GPU scaling, CI | binding |
| [rules/VLM_CALL_RULES.md](rules/VLM_CALL_RULES.md) | Binding rules for every model call | binding |
| [stages/README.md](stages/README.md) | Stage overview and order | proposal |
| [stages/K0-sync.md](stages/K0-sync.md) | Scan, diff, plan, guard, snapshot, publish, rollback | proposal |
| [stages/K1-embed.md](stages/K1-embed.md) | Image hashes and embeddings, concept text embeddings | proposal |
| [stages/K2-observe.md](stages/K2-observe.md) | One image per call, observations per facet | proposal |
| [stages/K3-cards.md](stages/K3-cards.md) | Concept cards, stability labels, variants, lint | proposal |
| [stages/K4-aliases.md](stages/K4-aliases.md) | Offline aliases and collision check | proposal |
| [stages/K5-index.md](stages/K5-index.md) | Derived serving index per snapshot | proposal |
| [stages/P-profiles.md](stages/P-profiles.md) | Profile registration, manifest, lint, golden tests | proposal |
| [stages/S0-intake.md](stages/S0-intake.md) | Effective configuration, image decode, snapshot pin | proposal |
| [stages/S1-candidates.md](stages/S1-candidates.md) | Alias, lexical and dense candidates, rank fusion | proposal |
| [stages/S2-planner.md](stages/S2-planner.md) | Intent IR, concept links, constraint ledger | proposal |
| [stages/S3-knowledge.md](stages/S3-knowledge.md) | Scope filter, stability policy, Knowledge Pack | proposal |
| [stages/S4-writer.md](stages/S4-writer.md) | Writer call with the admin system prompt | proposal |
| [stages/S5-validate.md](stages/S5-validate.md) | Structural checks, verifier, repair, fallback | proposal |
| [stages/S6-envelope.md](stages/S6-envelope.md) | Response envelope | proposal |
| [stages/G-evaluation.md](stages/G-evaluation.md) | Evaluation set, metrics, fitting, experiments | proposal |
| [note/README.md](note/README.md) | Index of research notes with verification class | maintained |

Language rule: everything is English except verbatim multilingual data (R-022).
