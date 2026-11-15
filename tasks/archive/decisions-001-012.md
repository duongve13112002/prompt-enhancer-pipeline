# Decisions 1 to 12 (archived verbatim from tasks/DECISIONS_INDEX.md on 2026-11-15)

Moved to keep the index within its character budget; the rows are unchanged and still in force unless a later decision replaces them.

| ID | Date | Decision | Source | Applied |
| --- | --- | --- | --- | --- |
| 1 | 2026-11-11 | Language: every repository file is English; chat with the user is Vietnamese | user answer | applied: AGENTS.md |
| 2 | 2026-11-11 | This repository holds documentation, configuration examples, contracts, a backlog and the agent tooling; no pipeline code; delivered as a zip | user answer ("Đúng rùi ha") | applied: README.md |
| 3 | 2026-11-11 | Two tasks: generate from a text prompt, and edit from a text prompt plus one source image | user proposal v1.1 | applied: tasks/REQUIREMENTS.md, docs/architecture.md |
| 4 | 2026-11-11 | The admin configures the system prompt, which defines the output format; one profile per image model; a request carries its profile id, else the admin's default profile applies | user answer | applied: docs/stages/P-profiles.md, configs/enhancer.example.yaml |
| 5 | 2026-11-11 | An image model may have separate system prompts for generate and edit; an edit request sent to a generate-only profile is refused, never reformatted | user answer | applied: docs/stages/P-profiles.md |
| 6 | 2026-11-11 | Each system prompt has a manifest (configuration, not rules in code), drafted by a model at registration and confirmed by the admin | user answer ("Có nha") | applied: docs/stages/P-profiles.md, contracts/schemas/profile_manifest.v0.schema.json |
| 7 | 2026-11-11 | No width, height or aspect ratio in the enhancer contract; bounding boxes are the profile's choice | user proposal v1.1 | applied: tasks/REQUIREMENTS.md |
| 8 | 2026-11-11 | Creativity is a configuration of what may be added, not only a temperature | user proposal v1.1 | applied: configs/enhancer.example.yaml |
| 9 | 2026-11-11 | A knowledge-base folder declares one or more concepts: line 1 of the info file holds the names, line 2 the descriptions, both split by a configurable separator (default the vertical bar) and matched by position | user answer ("kimono, donut") | applied: kb/README.md, configs/enhancer.example.yaml |
| 10 | 2026-11-11 | The same concept name in several folders is one concept | user answer | applied: contracts/README.md |
| 11 | 2026-11-11 | (Superseded by decision 30.) A folder without an info file takes its concept names from the folder name, split by the same separator; the description is empty | user answer | applied: kb/README.md |
| 12 | 2026-11-11 | Retrieval is on by default; switched off, the request path uses no knowledge base at all, cached knowledge included | user proposal v1.1 | applied: docs/architecture.md, configs/enhancer.example.yaml |
