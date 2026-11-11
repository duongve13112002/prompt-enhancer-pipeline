# Implementation Plan

Plan only. **Implementation (T-005 onward) starts only after an explicit user command** (AGENTS.md). T-002 to T-004 are read-only or documentation work that starts when the user approves its Step 0. No duration is estimated: it depends on the knowledge-base size, the machines and review capacity.

## Phase 0 and first working path

| ID | Task | Kind | R-IDs | Depends on | Needs user go |
| --- | --- | --- | --- | --- | --- |
| T-001 | Seed the repository and bind the copied tooling: **done 2026-11-11** | docs | R-021 | - | done |
| T-002 | Environment report (read-only; brief: [active/T-002](active/T-002-environment-report.md)): CPU, RAM, OS, Python, pip mode, disk for `data_root`, GPU and driver, llama.cpp build, installed library versions | scout | R-019, R-020 | - | Step 0 approval |
| T-003 | Evaluation set: scenario generators over the user's knowledge base (read only), answer keys from folder declarations, a sample for the user to review ([G](../docs/stages/G-evaluation.md)) | docs, then feature | R-023 | knowledge-base path | Step 0 |
| T-004 | Cold read of the documentation by Haiku, Sonnet and Opus readers, fixes, questions to the user: **first round done 2026-11-11** ([archive/T-004](archive/T-004-cold-read.md)); repeated after every restructuring | scout | R-028 | - | Step 0 approval |
| T-005 | Profile schema and loader: refuses every sentinel in the keys a command reads, unknown keys and missing fields; validates VLM roles ([contracts/vlm-request.md](../contracts/vlm-request.md)) | feature | R-015, R-004 | - | yes |
| T-006 | Storage layer: Parquet and Arrow tables, content-addressed artifacts, snapshot build, publish and rollback | feature | R-014, R-016, R-017 | T-005 | yes |
| T-007 | K0 sync: scan, diff, plan, mass-deletion guard, info-file parsing, folders without an info file recorded with a pending name | feature | R-007, R-014 | T-006 | yes |
| T-008 | K1 embeddings for images and concept text | feature | R-016 | T-007 | yes, dev GPU |
| T-009 | Model client: provider adapters, capability probe, warn-and-continue, structured-output validation; tested against the dev server (E12) | feature | R-018 | T-005, T-002 | yes, dev GPU |
| T-010 | K2 info-file assessment and folder naming by a model (decisions 30, 39, 40, E17) and observe, K3 cards with the lint report, `kb audit` | feature | R-007, R-010 | T-008, T-009 | yes, dev GPU |
| T-011 | K4 aliases and K5 serving index | feature | R-015, R-016 | T-010 | yes |
| T-012 | P profiles: register, manifest drafting, conflict lint, golden tests | feature | R-004 | T-009 | yes |
| T-013 | S0 intake and S1 candidates, with the retrieval-off isolation test | feature | R-009 | T-011 | yes |
| T-014 | S2 planner and S3 knowledge assembly | feature | R-001, R-008, R-013 | T-013 | yes, dev GPU |
| T-015 | S4 writer, S5 validation, S6 envelope | feature | R-024, R-027 | T-014, T-012 | yes, dev GPU |
| T-016 | Command-line entry points and the run guide for the user's deployment | feature | R-020 | T-015 | yes |
| T-017 | Pilot on the dev machine: every scenario end to end | run | R-020 | T-016, T-003 | yes |

**Gate 1:** every path (generate, edit, retrieval off) works end to end on the dev machine; structural checks pass; the isolation test passes with no knowledge base present.

## Workflow and tooling (decision 24, decision 26)

| ID | Task | Kind | R-IDs | Needs user go |
| --- | --- | --- | --- | --- |
| T-020 | Rebind the test-set rules in `brief_lint.py` to this repository's suites, with tests | code | R-021 | yes |
| T-021 | Brief order (context first, task last) in `brief_lint.py`; scope-guard hook that blocks edits outside a brief's Scope; report lint that requires the done-check output and evidence pointers | code | R-021 | yes |
| T-022 | CI: GitHub Actions on the latest Ubuntu and Windows; the workflow started by manual dispatch on a commit the user pushed (decision 37); the visibility-window script with the always-run return to private; a session-start check that the repository is private. Starts by re-reading the sources of [N134](../docs/note/N134-github-visibility-and-actions.md) ([docs/ci.md](../docs/ci.md)) | code | R-025 | yes |
| T-023 | Baseline of rule violations and tokens per accepted task, then the comparison after T-020 and T-021 | run | R-021 | no |
| T-024 | Align the copied tooling with decision 37 (the user makes every commit and push): `tool_guard.py` denies commit, push and history-changing commands for the PM as well as for workers; integration of a worker's result as an uncommitted patch; messages and docs that say "the PM commits" (`cli_worker.py`, `CLAUDE_CLI_SUBAGENTS.md`); tests | code | R-021 | yes |

## Phase 2: quality

| ID | Task | Kind | R-IDs |
| --- | --- | --- | --- |
| T-030 | `eval fit`: experiments E1 to E17 that fit every `fit_from_*` value, each with an evidence report | feature | R-015, R-023 |
| T-031 | Deployment smoke and latency and throughput measurement (E13), run by the user from the run guide | run | R-019 |
| T-032 | Card audit (E16) on a sample the user checks | run | R-010 |
| T-033 | Downstream evaluation with an image model, once the user provides one (E8) | run | R-011 |

**Gate 2:** intent preservation and concept resolution meet targets the user sets after the first measurements; latency meets R-019 on the deployment.

## Phase 3: extensions

| ID | Task | Kind |
| --- | --- | --- |
| T-040 | A distilled writer per profile from accepted traces (optional) | feature |
| T-041 | Edit extensions: several source images, masks, user-supplied reference images | feature |

Experiments E1 to E17 and their acceptance criteria: [docs/stages/G-evaluation.md](../docs/stages/G-evaluation.md).
