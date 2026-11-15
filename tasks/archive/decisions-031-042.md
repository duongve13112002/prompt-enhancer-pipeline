# Decisions 31 to 42 (archived verbatim from tasks/DECISIONS_INDEX.md on 2026-11-15)

Moved to keep the index within its character budget; the rows are unchanged and still in force unless a later decision replaces them.

| ID | Date | Decision | Source | Applied |
| --- | --- | --- | --- | --- |
| 31 | 2026-11-11 | Folders that share a stem do not form a family by rule; whether several concepts fit one mention depends on context | user answer | applied: docs/stages/S1-candidates.md, docs/stages/S2-planner.md |
| 32 | 2026-11-11 | The info file may be named `infor.txt` or `info.txt`; the accepted names are configuration | user answer | applied: configs/enhancer.example.yaml, kb/README.md |
| 33 | 2026-11-11 | Admin system prompt files are supported in both forms: sectioned (`[META]`, `[SYSTEM]`, `[USER]`) and plain | user answer | applied: docs/stages/P-profiles.md |
| 34 | 2026-11-11 | Targets for concept linking, latency and card precision are set by the user after the first measurements | user answer | applied: docs/stages/G-evaluation.md, tasks/STATUS.md |
| 35 | 2026-11-11 | The user creates the GitHub repository, clones it and sets up `gh` and everything else before agents start; agents work inside that clone | user answer | applied: docs/ci.md, tasks/STATUS.md |
| 36 | 2026-11-11 | The user makes the first commit themselves (extended by decision 37). Superseded by decision 77 | user answer | applied: tasks/STATUS.md, AGENTS.md |
| 37 | 2026-11-11 | The user makes every commit and every push, not only the first; no agent commits, pushes or merges. Superseded by decision 77 for the orchestrator; still holds for workers | user answer ("tui tự làm") | applied: AGENTS.md, .agents/policy/PM_POLICY.md, docs/ci.md |
| 38 | 2026-11-11 | A concept name produced by a model for a folder is used at once, marked as generated, listed in the sync report and correctable in the override file | user answer ("Ok") | applied: docs/stages/K2-observe.md, kb/README.md |
| 39 | 2026-11-11 | Folder naming runs when a folder has no info file or its info file is unclear and the folder name does not fit: the vision model sees n images drawn at random (default 5, configurable; all of them when the folder has fewer; a folder with no image is skipped) and returns the topic or topics and a description | user answer | applied: configs/enhancer.example.yaml, docs/stages/K2-observe.md, kb/README.md, tasks/REQUIREMENTS.md |
| 40 | 2026-11-11 | A model also judges whether each info file is clear; an unclear one sends its folder to the naming step | user answer ("thêm tự đánh giá") | applied: docs/stages/K2-observe.md, configs/enhancer.example.yaml, contracts/schemas/info_assessment.v0.schema.json |
| 41 | 2026-11-11 | Agents make no commit anywhere, temporary worker branches included; a worker's result reaches the main working tree as an uncommitted patch. Superseded by decision 77 for the orchestrator; still holds for workers | user answer ("Ko nha") | applied: AGENTS.md, .agents/policy/PM_POLICY.md |
| 42 | 2026-11-11 | When the sampled images of a folder show unrelated subjects, the model says so and the sync report flags the folder; nothing splits a folder | user answer ("Ok") | applied: docs/stages/K2-observe.md, contracts/schemas/folder_naming.v0.schema.json |
