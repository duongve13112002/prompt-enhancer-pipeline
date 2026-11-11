# Stage documents

Each stage document has the same sections: purpose, input and output, method, decisions and configuration keys, compute, dependencies, failure modes, experiments to run, references (note IDs in [docs/note/](../note/README.md)). Configuration keys refer to [configs/enhancer.example.yaml](../../configs/enhancer.example.yaml) (deployment) and [configs/enhancer.dev.example.yaml](../../configs/enhancer.dev.example.yaml) (dev machine). Tags: **[A]** assumption to verify, **[P]** PM proposal. Experiments E1 to E17 and their acceptance criteria are in [G-evaluation](G-evaluation.md). Offline order: K0, K1 image embeddings, K2 (its first step names folders without an info file), K3, K4, K1 concept-text embeddings, K5 (the concept text includes the card summary from K3 and the aliases from K4). Nothing is implemented or measured.

| Plane | Stage | Document |
| --- | --- | --- |
| Offline (`kb sync`) | K0 | [K0-sync.md](K0-sync.md) |
| | K1 | [K1-embed.md](K1-embed.md) |
| | K2 | [K2-observe.md](K2-observe.md) |
| | K3 | [K3-cards.md](K3-cards.md) |
| | K4 | [K4-aliases.md](K4-aliases.md) |
| | K5 | [K5-index.md](K5-index.md) |
| Profiles | P | [P-profiles.md](P-profiles.md) |
| Online (per request) | S0 | [S0-intake.md](S0-intake.md) |
| | S1 | [S1-candidates.md](S1-candidates.md) |
| | S2 | [S2-planner.md](S2-planner.md) |
| | S3 | [S3-knowledge.md](S3-knowledge.md) |
| | S4 | [S4-writer.md](S4-writer.md) |
| | S5 | [S5-validate.md](S5-validate.md) |
| | S6 | [S6-envelope.md](S6-envelope.md) |
| Evaluation | G | [G-evaluation.md](G-evaluation.md) |
