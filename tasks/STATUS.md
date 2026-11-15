# Implementation Status

Current state only (character budget: [doc_budgets.json](../tools/agent-workflow/doc_budgets.json)). One short row per active, blocked or pending item; no history. Decisions: [DECISIONS_INDEX.md](DECISIONS_INDEX.md). Snapshot: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). Plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current state

- **Phase:** `IMPLEMENTATION` (decision 95). The first pipeline code, the T-005 loader, is accepted. T-006 (storage, [task](active/T-006-storage-layer.md)) and T-009 (model client, [task](active/T-009-model-client.md)) are in progress (decision 96); T-024 is accepted.
- **2026-11-14 kickoff, T-002, T-003 and decisions 54 to 74:** done; moved verbatim to [status-history-2026-11-14](archive/status-history-2026-11-14.md).
- **GPU peer (decision 82):** a session of another project of the user shares the GPU (name in the PM continuation note); it gets "starting GPU job" and "GPU free" from the orchestrator. It reported no GPU job planned on 2026-11-14.
- **Done 2026-11-11:** repository seeded (T-001): requirements R-001 to R-028, decisions 1 to 53, architecture, fifteen stage documents, contracts and schemas, example profiles, an example admin profile, knowledge-base and evaluation-set layouts, 72 research notes; tooling copied and bound through `.agents/project.json`; rules restructured per decision 24 (core rules, role packs, CLI wrappers; map in [docs/agents/rule-map.md](../docs/agents/rule-map.md)).
- **Workflow revised 2026-11-13 (decisions 43 to 48):** the orchestrator cleans `.tmp` itself; Claude workers stop at 90% of Claude's window; scheduled wake at a known quota reset, resuming only approved tasks; one fresh worker per task; Codex image generation as a stand-in image model (T-034); AGENTS.md shortened and ORCHESTRATION rewritten for this repository's tools. Evidence: [N135](../docs/note/N135-quota-reset-and-scheduling.md), [N136](../docs/note/N136-codex-image-generation.md), [N137](../docs/note/N137-orchestration-guidance-review.md).
- **Cold read (T-004, R-028):** see the task file [archive/T-004-cold-read.md](archive/T-004-cold-read.md) for the first round's readers, findings, fixes and the questions sent to the user.
- **Moved 2026-11-15 18:45:** the decision 75 dispatch, the 2026-11-14 quota note, the seeding tooling check and the seeding "not run" row are in [status-history-2026-11-14](archive/status-history-2026-11-14.md).
- **Git and remote:** the user created the GitHub repository and set up `gh` (decision 35); since decision 77 the orchestrator commits and pushes verified work itself (no force push), and workers never commit. The delivered zip holds the files only, with no Git data.
- **T-025, T-024 and T-020 (tooling) accepted 2026-11-14/15:** their rows are in [status-history-2026-11-14](archive/status-history-2026-11-14.md).
- **T-005 (loader) accepted 2026-11-15, with the PM decisions the user accepted (decisions 103, 104):** moved verbatim to [status-history-2026-11-14](archive/status-history-2026-11-14.md).
- **State at 18:45 on 2026-11-15 (`Get-Date`).**
  - **T-009 (model client): accepted and pushed (`6b03510`);** details are in [status-history](archive/status-history-2026-11-14.md).
  - **E12 (dev GPU smoke): done at 21:08, GPU live, smoke only.** Model `unsloth/Qwen3.5-4B-GGUF` rev `e87f1764` (hashes match `models/manifest.json`) on llama.cpp `b11921` CUDA 13.4. All five combinations of docs/dev-machine.md met target on synthetic images: one image, JSON schema, image plus schema, thinking off, two images in order. Peak VRAM 3676 MiB; teardown back to 0 MiB. Limits: no thinking-on control (the template defaults to non-thinking); not more than two images. Evidence: `runs/E12-20261115-210633/summary.md`.
    - Findings: the server reports `reasoning_format: none`, so any thought text would land in `message.content` (the E10 observer must look there). The server warns that Qwen-VL needs `--image-min-tokens 1024` for grounding. **PM decision pending user review:** the next dev run sets it, since it changes only the dev server.
  - **T-012 (P profiles): accepted 2026-11-15 23:1x** (Opus build, 2 fix rounds; Codex, Gemini and Sonnet reviews; history in the status archive). Full pipeline suite on main: 2017 passed, 31 skipped.
    - P3 follow-ups: R2-1 a 100000-bracket `activation.json` leaks `RecursionError` (`profiles.py:645`, fails closed); R2-2 the lost-edit window is one fsync wide; R2-3 confirm refuses a CRLF or BOM manifest; R2-4 a leftover `check.lock` for an unknown id; R2-6 no test of the addendum clause; R2-7 register can lose a paid draft when the lock is busy after the model call. **PM decision pending user review (R2-5):** `check_code_sha256` hashes all of `config.py`, so any edit there, comments included, forces a new paid golden run; kept fail-closed until you choose a narrower scope. **PM decisions pending user review:** a code fingerprint and schema hashes in activation; raw NEL, LS and PS refused in admin YAML; confirm rewrites only its own writer form; one bundle lock for register and confirm (`PROFILE_BUSY`).
    - **PM decision pending user review:** META types only the YAML 1.2 core `true`/`false` and decimal numbers. `yes`, `no`, `on`, `off`, dates and leading-zero numbers stay strings, exactly as written.
  - **T-006 race follow-up and T-006-int: accepted 2026-11-16 00:1x.** Every file mutation in `src/` goes through the anchored layer `storage/_anchored.py` (decision 123 store lock on release; profile bundles anchored at the project root; `publish_new_bytes` no-replace). Reviews: race retest 3 Sonnet PASS and Gemini PASS; T-006-int Sonnet PASS (31 own junction probes refused) and Gemini PASS. Task files: [T-006-race-followup](archive/T-006-race-followup.md), [T-006-int](archive/T-006-int.md); history in the status archive. P3 follow-ups: the AST guard misses `builtins.open`, `io.FileIO`, numeric `os.open` flags, `.save(path)`, nested pyarrow writers and non-literal `.open(mode)` (no such code today); `_ensure_root` in `_write_bytes` untested; the segment refusal reaches `resolve_profile` as a raw `StorageError`, to map when the profile commands are wired; POSIX `linkat` branches run only on Linux CI. **PM decisions pending user review:** round 3 covers the whole class; a failed `release()` becomes a note or `StorageWarning`; profile folder names failing `check_segment` are refused.
  - **T-008 (K1 embeddings): accepted 2026-11-16 00:46** (Opus build, 5 fix rounds under decision 121; Codex retest 5 PASS, Gemini PASS; round history in the status archive). Main suite with T-008: 2900 passed, 42 skipped. Integration: `config.py` import merged by hand; K1's `_commit` renamed `_commit_record` (the anchored-writes AST guard refuses the removed helper's name). Follow-ups: N3 P2 (T-009) ICO under 16 px gives an empty container; N4 P3 MPO labelled `image/mpo` with JPEG bytes; N5 P3 PALM and XBM cannot encode RGB (refused as `encode_failed`); N2 P3 integral floats outside K1's read set. **PM decision pending user review:** the loader applies the finite-float timer rule to every backend.
    - **PM decisions pending user review:**
      - The embedder is a named `vlm.backends` entry.
      - There is no `embedding.devices` key: the server launch places the model.
      - The llama.cpp and vLLM image adapters are both built, and the request shape stays `design_required` until a live smoke.
      - Pixel size is stored before EXIF orientation.
      - A referenced artifact is reused.
    - The dev pairing, EmbeddingGemma 2 on llama.cpp, has no primary confirmation. Qwen3-VL-Embedding-2B on vLLM has an official example. This goes to you with E5.
  - **T-021 (tooling): phase P1 (brief order) accepted 22:2x after fix round 2.** One shared block scanner reads every brief; quote, list and HTML containers are tracked. Codex retest 2 PASS (F4, F5 closed; 4368 legacy comparisons match the base; 102 CRLF/BOM variants match) and Gemini PASS. Applied to main: focused 674 passed. Known conservative limit: lazy continuation followed by `---` is refused as a Setext heading. **PM decision pending user review:** an HTML block is refused only when it holds a heading, underline or fence, so the wrapper's `<goal>` tag lines still pass; no new Markdown library; the new-order lint refuses HTML blocks and headings or fences inside containers instead of parsing them. P2 (report lint) and P3 (scope guard) each follow after their own review. **PM answers, pending user review:**
    - **P2 (report lint) BLOCKED on the user, 2026-11-16 04:05:** fix round 4, the last extra round of decision 121, still FAILS the Codex retest 4 (`.tmp/worktrees/T-021-p2/.tmp/T-021/retest4-p2/review.md`), while Gemini passes it.
      - The two P2 findings are R4F1, where code or HTML inside an image description in a heading skips report discovery, and R4F2, where paragraph rebuilding and heading scans are quadratic and a watchdog fires near 400 KB.
      - Each round closed its own findings and the next retest found a new construct; rounds 3 and 4 also found new defects (R3F1 backtracking, R4F2 quadratic).
      - The patch stays unintegrated in its worktree. Nothing is reverted.
      - **Question for the user:** a narrow round 5, a narrower lint contract, or a CommonMark parser dependency in `.venv`.
    - `--scope <glob>` is a registry field (P3).
    - `.agents/policy/cli/README.md` and `claude.md` join the scope; the PM edits `PM_POLICY.md:65` itself.
    - A registry marker keeps old workers on the old order.
    - The report lint stays advisory.
    - No rebase: base-only failures are reported.
  - **T-007 (K0 sync): accepted and pushed.**
    - The Sonnet re-review passed, and so did its check of fix round 2.
    - Codex combined T-007 with T-009. The test profile is now built from `enhancer.example.yaml` plus a shared `tests/conftest.py` helper.
    - Main pipeline suite: `1824 passed, 31 skipped`. A Pillow deprecation warning remains: saving `I` mode PNGs, at `model_image.py:80`, is a T-009 follow-up.
  - **T-022 (CI): accepted 2026-11-15 21:50 after fix round 4;** the row, with its P3 follow-ups (hard-linked ledger, hold only the leaf, `docs/ci.md:66` text), is in [status-history](archive/status-history-2026-11-14.md). The first window (22:15) refused before any change: `docs/ci.md:197` of `e9b2781` quoted the fake test key. **PM decision pending user review:** that line is baselined and the text now describes the value instead.
  - **T-026 (quota): accepted and pushed (`925e930`);** details are in [status-history](archive/status-history-2026-11-14.md).
  - **S42 Antigravity runner: accepted and pushed.**
    - Round 2 closed RF1 and RF2.
    - Round 3 wraps every collection read in `@(...)` after a class audit, which closed R2F1 (a scalar CIM `Count` in `undo.ps1`).
    - The Codex retest 3 PASSED; `test_agy_lib.py`: 149 passed.
    - Its P3s are in the S42 queue.
    - Before you run `setup.ps1`, see question 6.
  - **S42 CI reds: accepted and integrated 2026-11-16 04:30.** Fix rounds 1 and 2 close the CI-red tool tests on both readings, under POSIX and Windows. Reviews: Sonnet review 3 PASS, Gemini fix 2 PASS.
    - Main tool suite after the patch: `5394 passed, 11 skipped, 531 deselected in 909.97s`.
    - Codex hook files, run with a scratch `AGENT_WORKFLOW_STATE_DIR`: `530 passed, 1 skipped`.
    - **Residuals:**
      - A command inside both bounds can outlast the hook timeout (24.9 s in worker mode, 30.9 s in PM mode with 14 pushes), because there is no whole-evaluate wall-clock budget.
      - Hooks fail open on timeout, with no `onFailure: block` (question for the user).
      - A budget-per-target mutant survives.
      - The bound 512 was calibrated on one machine.
    - **PM decision pending user review:** both readings on POSIX; the bounds 64 and 512.
  - **S42 PM fix:** `AGENT_BRIEF_FOOTER.md` item 11 now says "Reply in English: your reader is the PM, not the user", because workers had taken the PM for the user under AGENTS.md section 1. 191 related tests passed. Another family still has to review it.
  - **S42 queue:**
    - `brief_lint` false positive: task-file text such as "every commit reachable" is flagged as a git write;
    - `gemini-ws.ps1` reads files fail-open;
    - `tool_guard` should block a PM Bash `cd` below the project root (recurred 2026-11-15 20:2x);
    - `test_slot.py` crash-dialog suppression: done and Codex-reviewed (`SetErrorMode` in `main()`); `cli_worker.py` still needs the same;
    - the ledger false positive when another worktree is removed;
    - Codex temp dirs that cannot be removed;
    - the runner's R3F1 (the AST lint misses `$x = (Get-X)` and member targets) and R3F2 (the audit's false claim that a scalar PSCustomObject has `Count`);
    - the pre-push check misses the machine's own absolute root in tracked files (lesson 2026-11-15);
    - `test_cli_worker_codex.py` and `test_codex_hooks.py` read the live `.tmp/pm-state` ledger, not a test state dir: in main, 42 tests fail with `git_history_changed`. They pass on a clean HEAD, and they pass in main with `AGENT_WORKFLOW_STATE_DIR` set to an empty folder (2026-11-15 18:4x). The override itself breaks 26 other tests (`test_brief_lint` send, `test_pm_supervisor`, a `test_session_context` test), which expect the default state dir: no single setting runs the whole suite green. Fix: each test gets its own state dir.
  - **Follow-ups:** T-006 storage gets lease reclaim by owner identity, deferred from T-007; the handover from `pin()` to the caller is a documented known limit.
  - **PM decisions of 2026-11-15, all accepted by the user (decision 119); the rows elsewhere in this file that still say "pending user review" are accepted too:**
    - **T-022 RT1 rule, T-022, T-009 and T-007 decisions (all accepted):** moved verbatim to [status-history](archive/status-history-2026-11-14.md).
    - **T-026:** when the file is malformed, nothing is written and the exit code is 1. A valid file is re-rendered canonically. Readers show the valid entries plus one diagnostic.
    - **T-012 Step 0:**
      - Conflicts may cite `config_keys`, and the lint checks them structurally.
      - The runtime addendum is rendered from the template `runtime_addendum_v0` and recorded by hash at check.
      - Register refuses to overwrite an existing manifest.
      - Check writes `activation.json` and never rewrites the admin's manifest.
      - Golden cases use a new `golden_case.v0`. A pass means a valid envelope whose status is in the expected set (v0, weak on purpose until S5).
      - The input variable is the `[USER]` placeholder without a `template_values` entry.
      - The drafter prompt `manifest_drafter_v0` text is written by the builder.
    - **S42 runner:** the task name hashes the upper-cased canonical root. Binding refuses bare or relative wrappers but still counts them as runners. Exactly one `powershell.exe` action. Any mention of `agy-job.ps1` counts as a runner.
- **Earlier plan (decision 95).** Measured on 2026-11-15: a fix round takes 35 to 60 min, a Codex retest 15 to 40 min, a Gemini review 5 to 20 min.
  - **T-024, T-006, and the earlier T-009 build history:** moved verbatim to [status-history-2026-11-14](archive/status-history-2026-11-14.md). T-006 residual follow-up: `DataStore.read` lacks an internal-link test. Deployment note: `storage.lock.timeout_s` grows with snapshot size.
  - **T-009 `engine` key decision:** accepted by the user (decision 119); the full text was moved verbatim to [status-history](archive/status-history-2026-11-14.md).
  - **Closed S42 fixes (stdin briefs, contract-drift docs):** in [status-history](archive/status-history-2026-11-14.md). The Gemini implementation smoke waits on question 6.
  - **Pipeline first (decision 100; the user asked on 2026-11-15 why tooling took so much time):** after T-024 lands, new tooling work waits behind the pipeline queue. Only defects that block pipeline work are fixed at once (S42).
  - **Pipeline queue:** T-010 (K2 naming and observation, K3 cards, `kb audit`) in three phases; Codex builds P1 after two pre-mortems (Gemini, Claude Sonnet); the task file's PM decisions before P1 are pending user review. P1 Step 0 approved 2026-11-16 01:01 (Codex `t010-p1-codex-dev` building). **PM decisions pending user review** (answers to its Step 0 Q1-Q5, in `.tmp/T-010/p1-approve.md` and the task file): model-facing `info_assessment.v0` and `folder_naming.v0` without pipeline ids, with a positional `assigned_descriptions`; schema `kb_overrides.v0` for the four documented keys; assessment off plus a mismatched description line is refused; failed K1 decode statuses skipped, null passes; single-behaviour settings are audited as constrained exceptions. E12 live smoke after P3.
  - **Tooling in parallel (decision 105):** T-026 is accepted, and T-021 runs beside the pipeline queue. The old T-026 timeline was moved verbatim to [status-history](archive/status-history-2026-11-14.md).

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
6. **Done 2026-11-15 (decision 116):** the user cleared the Deny rules and ran setup. `setup_project.py check` shows task `AgyRunner-prompt-enhancer-pipeline-fbff4b2a` bound to this root, `.tmp/agy-jobs` present, and Allow Modify on `.tmp/agy-work`: 23 ok, 0 warn.
