# Implementation Status

Current state only (character budget: [doc_budgets.json](../tools/agent-workflow/doc_budgets.json)). One short row per active, blocked or pending item; no history. Decisions: [DECISIONS_INDEX.md](DECISIONS_INDEX.md). Snapshot: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). Plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current state

- **Phase:** `INIT` (documentation and agent tooling; no pipeline code). Implementation starts only on an explicit user command.
- **Done 2026-11-11:** repository seeded (T-001): requirements R-001 to R-028, decisions 1 to 42, architecture, fifteen stage documents, contracts and schemas, example profiles, an example admin profile, knowledge-base and evaluation-set layouts, 72 research notes; tooling copied and bound through `.agents/project.json`; rules restructured per decision 24 (core rules, role packs, CLI wrappers; map in [docs/agents/rule-map.md](../docs/agents/rule-map.md)).
- **Cold read (T-004, R-028):** see the task file [archive/T-004-cold-read.md](archive/T-004-cold-read.md) for the first round's readers, findings, fixes and the questions sent to the user.
- **Tooling check at seeding:** `python3 -B -I tools/agent-workflow/setup_project.py check` in a Linux container: binding, instruction files, budgets, hook-read files, Git-ignore entries and foreign tokens OK; warnings for machine facts (no `.venv`, no Codex or agy CLI, no PowerShell, hooks not registered, no `.tmp/pm-state`); `setup_project.py apply` was not run because it renders machine-local settings.
- **Not run:** nothing was implemented, measured or benchmarked. No live Claude, Codex or agy worker was started in this layout; hooks were not exercised in an interactive session. The tool test suite was not run here.
- **Git and remote:** the user creates the GitHub repository, clones it, sets up `gh` and makes every commit and push (decisions 35 to 37 and 41); agents work inside that clone and only propose commit messages. The delivered zip holds the files only, with no Git data.
- **Next:** T-002 (environment report, read-only) and T-003 (evaluation scenarios from the user's knowledge base), each when the user approves its Step 0.

## PM proposals in force, awaiting the user's explicit confirmation

1. The role-by-facet scope matrix, the stability labels of concept cards and the three edit kinds ([S3](../docs/stages/S3-knowledge.md), [K3](../docs/stages/K3-cards.md), [S2](../docs/stages/S2-planner.md)); each has an experiment in [G](../docs/stages/G-evaluation.md).
2. The model verifier runs inline only for the risk group and on a sample otherwise ([S5](../docs/stages/S5-validate.md)).
3. CI runs both runners on every run of the visibility window ([docs/ci.md](../docs/ci.md)); the alternative is Windows only at feature completion and before a release.
4. Brief order "context first, task last" and the scope-guard and report-lint tools wait for tooling tasks T-020 to T-022; until then briefs keep the old order.
5. CI starts by manual dispatch only, when the user asks for a run on a pushed commit ([docs/ci.md](../docs/ci.md)).

The copied tooling still assumes that the PM commits; T-024 aligns it with decisions 37 and 41.

## Not questions for the user

Settled by experiments or by the implementing task: every profile value marked `fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required` or `design_required`; the embedder, the number of representative images per folder, thinking on or off, the quantisation file on the dev machine, shortlist sizes. Machine facts are measured by T-002. Deployment values (`deploy_required`) are filled in the deployment overlay.

## Questions waiting for the user

1. The path of the real knowledge base on the dev machine (T-003 starts with it; the repository holds only `kb/_example/`).
2. Before any paid cloud call, the PM asks for the number of calls allowed and the key file in `.secrets/`.
3. Targets for linking coverage (E1), latency on the deployment (E13) and card precision (E16): the user sets them after the first measurements (decision 34); the PM brings the numbers.
4. Open questions from the cold read: listed in the T-004 task file ([archive/T-004-cold-read.md](archive/T-004-cold-read.md)).
