# Implementation Status

Current state only (character budget: [doc_budgets.json](../tools/agent-workflow/doc_budgets.json)). One short row per active, blocked or pending item; no history. Decisions: [DECISIONS_INDEX.md](DECISIONS_INDEX.md). Snapshot: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). Plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current state

- **2026-11-20: T017-FIX3, CLOUD-1 and MS-1 integrated on main (decisions 194 to 207).** Notes: [implementation-notes](../docs/implementation-notes.md).
  - **T017-FIX3:** the writer and every S5 repair send the admin bundle's `output_schema.json` and validate against it (G1, decision 194); `golden_case.v1` with the retrieval switch; `response_envelope.v1` and `intake_result.v1` with `retrieval.error` and `not_reached`; `degraded` never passes a grounding case.
  - **CLOUD-1:** Anthropic structured output with a stripped sent schema, Haiku 5.5 400-rules refused at load, seedless retries, budget `stop`, `x-api-key` instead of Bearer. **PM decision pending user review:** numeric temperature, top_p and top_k in a Haiku role stay warned and unsent (`vlm_role.v0` accepts `none` only for seed and stop).
  - **MS-1:** multi-source contracts (`request.v1`, `intake_result.v2`, `planner_reply.v1`, `intent_ir.v1`, `plan_result.v2`) and prompt drafts; no runtime change.
  - **Server impact:** `model-call-6`, `k2-observe-4` and `k3-card-4` make K2, K3 and K4 recompute once at the next `kb sync`; v0 golden cases are refused, so profile golden cases move to `golden_case.v1`; envelopes are v1.
  - Reviews: each task Gemini Flash PASS and Claude Sonnet PASS, no fix round.
  - CI 40 failed on one stale test (`test_probe_retry`, the refusal decision 205 removed); CLOUD-1 fix round 1 (`a26ca13`, Gemini diff-only PASS) made CI 41 green; private again.
  - **T017-HAIKU (blocked on the user, decision 207):** the live probe stopped in the key loader (`api_key_file is missing or unreadable` for `.secrets/claude.api-key`); 0 of 3000 Haiku requests used. Ready in the Git-ignored pilot: `output_schema.json` (`$id` `pilot_v1.writer_output.v1`, since the loader refuses hyphens), the 3 golden cases on `golden_case.v1`, planner, writer and verifier bound to Haiku 5.5 (3000, `stop`). **PM decisions pending user review:** the knowledge-base roles stay local; `allow_images: true` (the edit case's source image goes to Anthropic); `max_concurrency: 1`; `smoke-edit-local-04` requires grounding.
  - **Next after the key check:** the probe (3 requests); record the approval gate (decision 208); golden J4 on Haiku; then the decision 189 chain.
  - Noticed, not fixed: `gh run cancel` and `gh run list` could meet the transient 404 that `ci_window` handles for `gh run view`; `.tmp/worktrees/T-026-retest3` could not be removed ("link or left partially"), not forced.

- **2026-11-20 02:5x: T-017 was blocked on the user; answered by decisions 194 to 196.**
  - **J4a5 (after T017-FIX2):** the live fix works. llama.cpp accepts the bounded schema; the planner retry differs only in its seed, and `ragoff-02`'s planner passed on the retry.
  - **Planner sampling experiment (decision 186):** two variants, 0.7/0.8/20 and 1.0/0.95/40, against the 0.2/0.9/40 baseline. Neither makes `nomatch-01` or `edit-local-04` pass. The 4B dev planner keeps breaking cross-field invariants: a knowledge need for an unlinked candidate, a relation or edit target naming an undeclared entity, and with hotter sampling also non-verbatim mentions (R-001 refuses them, as designed). This is a 3-case smoke, not a fit (R-020).
  - **G1:** `ragoff-02`'s writer reply still lacks its final `}`; the draft and the reseeded repair are byte-identical.
  - **Consequence:** J4 cannot be green, so the decision 189 chain (full `kb sync`, full pilot) stops here.
  - **Answered:** G1 by an admin output schema (194); quality on Claude Haiku 5.5, the local 4B model tests the local path only (195); MS-1 starts (196).
  - **Noticed:** the trace keeps only `INVARIANT_INVALID` for a refused planner reply, not which rule refused it.

- **2026-11-20 02:xx: T017-FIX2 integrated on main.**
  - The S2 planner retry and the S5 writer repair now change only the seed (`attempt_seed`).
  - The planner gets a reply schema bounded to the shortlist it was sent.
  - `model-call-5`: K2, K3 and K4 recompute at the next `kb sync`.
  - Reviews: Gemini Flash PASS, Claude Sonnet PASS WITH NITS; one fix round; Gemini retest PASS.
  - **PM decision pending user review:** a dropped seed refuses the planner retry, since it would repeat byte for byte, but never the writer repair. See [implementation-notes](../docs/implementation-notes.md).
  - Next: `kb sync` of the smoke-b store, then golden rerun J4a5.

- **2026-11-20 00:0x: J4a4 (golden rerun with the dev window budget 4096): G2 fixed, 3 of 3 cases still fail.** S1 is `ready` with 2 candidates in each case. `edit-local-04`: the planner names a relation to an entity it never declared. `nomatch-01`: a knowledge need for a candidate that no link supports; the retry is still the same sample, which T017-FIX2 fixes. `ragoff-02`: the writer JSON again lacks its final `}` (G1). R-004 holds: the admin `[SYSTEM]` bytes come first, unchanged. **Open for the user:** the envelope reports retrieval `failed` whenever S3 did not run (`envelope.py:29`), even when S1 was `ready` and the planner failed; the contract has no "not reached" state.

- **2026-11-20 00:xx: CI 36 red, test-only defect fixed.** Six `tests/test_addendum_layout_config.py` cases loaded a `tmp_path` child profile with the checkout as project root; they passed locally only because the test runner keeps its basetemp inside the checkout (CI: `/tmp` on Ubuntu, another drive on Windows). They now load from a small profile tree under `tmp_path` holding the two example profiles.
- **2026-11-20 00:xx: J4a3 root causes found (read-only Opus report, T017-RCA3).**
  - G1 `ragoff-02`: the dev model itself ended the writer JSON without its final `}`; no output schema was sent, because the admin bundle has none.
  - G2: S1 failed with `WINDOW_BUDGET_EXCEEDED`. The golden prompts need 707 to 2781 windows; the dev pilot value was 8. A snapshot was pinned (the task file's "no snapshot" was wrong). The envelope shows only `RETRIEVAL_FAILED`.
  - G3: with that empty shortlist, the planner invented a knowledge need at position 0. The edit case first broke the verbatim-span rule (as designed, R-001).
  - G4: the planner retry is byte-identical under a fixed seed.
  - **PM decisions pending user review:** dev pilot `candidates.max_prompt_windows` 4096 (Git-ignored profile; the examples keep `fit_from_bench`); task T017-FIX2: the planner retry and the S5 writer repair change only the seed (`attempt_seed`, as in K2 and K3), and the planner receives a reply schema bounded structurally to the shortlist it was sent.
  - **Open for the user:** an output schema in the admin bundle, or JSON mode derived from the admin's validators (G1); a retrieval switch in golden cases, so that `ragoff` really tests R-009; the S1 error code in the envelope; whether `degraded` may pass a grounding case.

- **2026-11-19 23:2x: T017-FIX integrated on main (decisions 190 to 193).** Per-backend `addendum_layout` and `addendum_separator` (dev Qwen joined with "\n\n", `none` only with the separate layout), the generate-only planner schema, failure codes kept through S2 into golden results and eval markers; merged with K3-RETRY as `model-call-4`. Reviews: Claude Opus PASS (three P3, fixed), Gemini Flash PASS, one fix round, Gemini retest PASS, Gemini merge review PASS. Full suite: tooling 6558 passed; pipeline 9114 passed, 1 failed (the K5 tie, fixed by K5-TIE next). Not verified until the live J4a2 re-run: the Qwen3.5 template with the joined message, llama.cpp grammar for the generate schema. **PM decision pending user review:** draft configuration schemas take additive keys in place; stage and stored-artifact schemas get a new version.
- **2026-11-19 23:3x: K5-TIE integrated on main.** `ServingIndex.search` scored shards with a BLAS matrix-vector product whose bits depend on the shard shape (`-2.2e-17` versus `0.0` for the same vector), so ties among equal vectors broke on float noise and the ranking depended on `block_rows`; now an elementwise product and a row sum over a C-contiguous matrix. Builder Claude Sonnet; Gemini Flash review PASS (two P3, fixed), retest PASS. See [implementation-notes](../docs/implementation-notes.md).
- **2026-11-20 01:4x: user answers (decisions 190 to 193); the T017-FIX PM decisions below are accepted.** Per-backend `addendum_layout` accepted, dev Qwen profile `joined_system_message` with separator "\n\n"; the plain `image` probe stays required and relies on the decision 188 retry, measured in the seed experiment; a planner failure fails the case and `docs/stages/S2-planner.md` is corrected to say so; `addendum_separator: none` is allowed only with `separate_system_message`, the base example states `deploy_required`. The last three go into the T017-FIX fix round.
- **2026-11-20 01:xx: K3-RETRY integrated on main (decisions 183 and 188; full suite before commit).** K3 cards, K2 observations and the plain `image` probe retry a refused reply with another seed; three attempts keys hold `fit_from_bench` until the seed experiment fits them. Reviews: Gemini Flash PASS, Claude Opus PASS with five P3, one fix round, Gemini diff-only retest PASS. Next: T017-FIX (building), then the seed experiment on the dev GPU and the T-017 chain.
- **History 2026-11-19 15:1x to 23:5x** (ATTACH-NOTES, P3-SWEEP-2, MULTI-SOURCE design, the approved-profile gate, T-017 J1 to J4a2, decisions 179 to 189): moved verbatim to [status-history-2026-11-20](archive/status-history-2026-11-20.md).
- **History to 2026-11-19 14:5x** (T-016 P1 to P5, K4-ALIASFAIL, KBS-S3-1, P3-SWEEP-1, CI windows 26 to 29, the setup-test copy defect): moved verbatim to [status-history-2026-11-19b](archive/status-history-2026-11-19b.md); earlier rows in [status-history-2026-11-19](archive/status-history-2026-11-19.md) and [status-history-2026-11-14](archive/status-history-2026-11-14.md).

## PM proposals in force, awaiting the user's explicit confirmation

1. The role-by-facet scope matrix, the stability labels of concept cards and the three edit kinds ([S3](../docs/stages/S3-knowledge.md), [K3](../docs/stages/K3-cards.md), [S2](../docs/stages/S2-planner.md)); each has an experiment in [G](../docs/stages/G-evaluation.md).
2. The model verifier runs inline only for the risk group and on a sample otherwise ([S5](../docs/stages/S5-validate.md)).
3. CI runs both runners on every run of the visibility window ([docs/ci.md](../docs/ci.md)); the alternative is Windows only at feature completion and before a release.
4. Brief order "context first, task last" and the scope-guard and report-lint tools wait for tooling tasks T-020 to T-022; until then briefs keep the old order.
5. CI starts by manual dispatch only: the PM opens the visibility window after each accepted and pushed task, or when the user asks (decision 112, [docs/ci.md](../docs/ci.md)). T-022 builds it now in parallel (decision 111); its session-start privacy check waits for T-026.
   - Its secret scan is gitleaks 8.30.1 (decision 113), at `.tmp/tools/gitleaks/8.30.1/bin/gitleaks.exe`. The zip's SHA-256 matched the release checksums. The exe's SHA-256 is `17157E2EE8B76FC8B1D8BEE607A250E34B8A8023C8BC81822D4B5EE4D78FCB7C`.
6. The writer gets `output_language` and, for an edit, the whole-edited-image semantics through the Enhancement Brief, never the system message ([S4](../docs/stages/S4-writer.md)); warning `CLARIFICATION_ASSUMED` for best-effort readings ([contracts](../contracts/README.md)). Accepted by the user 2026-11-14: decision 69's reading (decision 79) and `auto` for `{{aspect_ratio}}` (decision 81).

The copied tooling still lets workers in merge mode stage and merge, guards the orchestrator only against force pushes, names a missing pre-push script, assumes a worker registry file and expects `agy -p /usage` to print plain JSON; T-024 to T-026 align it.

## Not questions for the user

Settled by experiments or by the implementing task: every profile value marked `fit_from_gt`, `fit_from_bench`, `fit_from_pilot`, `pin_required` or `design_required`; the embedder, the number of representative images per folder, thinking on or off, the quantisation file on the dev machine, shortlist sizes. Machine facts are measured by T-002. Deployment values (`deploy_required`) are filled in the deployment overlay.

## Questions waiting for the user

1. **T-026:** answered 2026-11-15, round 4 (decision 114).
2. **E12 download: answered yes on 2026-11-15 (decision 115); the model is done, the llama.cpp build is in progress** (see the E12 row above).
3. Before any paid cloud call, the PM asks for the number of calls allowed and the key file in `.secrets/`.
4. Targets for linking coverage (E1), latency on the deployment (E13) and card precision (E16): the user sets them after the first measurements (decision 34); the PM brings the numbers.
5. Open questions from the cold read: listed in the T-004 task file ([archive/T-004-cold-read.md](archive/T-004-cold-read.md)).
6. **Done 2026-11-15 (decision 116):** Deny rules cleared and setup run; `setup_project.py check`: 23 ok, 0 warn.
8-12. **Answered 2026-11-16 20:5x (decisions 132 to 136):** occupied folder targets stay refused; dev embedder download and dev values allowed; numpy for K5; `aliases.languages` `[vi, en, ja, zh]` in the dev profile; PM decisions after decision 130 accepted.
13. **Answered 2026-11-16 (decision 137):** support both dev embedders (EmbeddingGemma 2, Qwen3-VL-Embedding-2B) to compare accuracy; Qwen3-VL-Embedding-8B supported in the profiles but never run on the dev machine. Scout (`.tmp/E12-embedder/research.md`, leads until the PM re-reads them): EmbeddingGemma 2 has an official `ggml-org` GGUF (Q8_0 plus projector about 0.87 GB) and llama-server documents image embeddings on `/v1/embeddings`, but no source names this model's vision path; Qwen3-VL-Embedding-2B has no official GGUF, llama.cpp's multimodal list has no Qwen3-VL, a community GGUF reports image cosine 0.967 against the reference, and a Windows crash issue exists. 14-15. **Answered (decisions 138, 139):** both dev embedders run from original weights through a project-owned `transformers` server in `.venv-gpu` serving `/v1/embeddings`; downloads allowed (cap 12 GB); after the dev comparison the better model's weights stay, the other's are deleted, both stay supported. New task **T-018** (Claude builds, Codex reviews; decision 91).
16. **Answered 2026-11-17 20:1x (decision 149, standing rule S46):** the resource rule is mandatory here. Task **RES-1** implements it: an audit of every model and embedder call path, followed by one fix round with differential tests. The audit covers K1 to K4, S2, the embedding server batching and the schema registry rebuild that costs about 50 s per `planner.plan` call. Every review checks it from now on.
7. **Answered 2026-11-16 (decisions 124 to 127):** a normalized-AST fingerprint (integrated 08:5x); a `kb_overrides` key `name` (T-010 P3); a guard wall-clock budget; CommonMark for T-021 P2.
