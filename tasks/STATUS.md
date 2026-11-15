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
- **T-005 accepted 2026-11-15:** the configuration and profile loader (`src/prompt_enhancer/config.py`, `tests/`; [task file](active/T-005-profile-loader.md)). Codex implemented it in 3 fix rounds (the cap). Sonnet 5.5 reviewed each round and Gemini 3.8 Flash reviewed round 2; the final Sonnet retest PASSES. Pipeline suite: 804 passed, 25 skipped. Not run: Linux, ruff, a real 8.3 short-name alias (short names are off on this volume). One open P3: `resolve_writer_format` names a bare file name, not the full path. **PM decisions from the fix rounds, accepted by the user (decision 103):**
  - Configured roots resolve inside the project, are not the project root itself, and lie outside `.secrets`, `.git`, `.venv` and `models`, compared after resolving links.
  - `api_key_file` is `none` or literally `.secrets/<name>`, case exact on every OS, and is never opened.
  - Trailing dots or spaces, surrounding whitespace and Windows device names are refused on every OS.
  - Relative `load_config` and `load_profile` arguments are project-root-relative.
  - The overlay may set `max_concurrency`.
- **T-005 build (decision 95):** a Haiku scout wrote the research note, then Codex implemented (Step 0 first) and Claude reviewed. **PM decisions, accepted by the user (decision 104):** the string `none` has one meaning everywhere, "this optional setting is off / not sent" (as in decision 84), so schemas that reject it for `seed`, `stop` and `budget_tokens` accept it; a whole-configuration schema `enhancer_config.v0` is written from the documents, including `clarification`; `unsupported_param_policy` is required, and the examples set `warn` (warn-and-continue, T-009); each command has an explicit read-set table and an unknown command is refused; overlays merge recursively (scalars and lists replaced, cycles refused); the package lives in `src/prompt_enhancer/` with tests under `tests/`; an overlay inherits non-deployment values such as `unsupported_param_policy` from its base; documented settings that have no key yet (K1 embedding sizes and devices, S0 deadlines) are listed in `contracts/README.md` as owned by T-008 and T-013; the writer's `format: from_profile_manifest` resolves to `json_schema` when the manifest names a schema and to `none` otherwise (`docs/stages/S4-writer.md:15`).
- **State at 18:45 on 2026-11-15 (`Get-Date`).**
  - **T-009 (model client): accepted and pushed (`6b03510`);** details are in [status-history](archive/status-history-2026-11-14.md).
  - **E12 (dev GPU smoke): blocked.** `models/` holds no model file and `llama-server` is absent from PATH. Downloading the dev model (decision 20: `Qwen3.5-4B-Q3_K_M.gguf` 2.29 GB plus `mmproj-F16.gguf` 0.67 GB) and a llama.cpp build needs the user's approval (question 2 below).
  - **T-012 (P profiles): fix round 1 running (19:3x).** Built by Opus: 84 P tests, 22/22 mutants. Both reviews FAILED.
    - **Codex** found F1 to F5:
      - F1 (P1): an overlapping failed check leaves the profile active.
      - F2: registration can overwrite an admin's manifest.
      - F3: META gets YAML 1.1 coercion.
      - F4: a cited `profile_wins` value can drift without a new check.
      - F5: the golden-results hash is never verified.
    - **Gemini** found F3 too, plus G2 (P2: a stray `}}` is accepted) and G3 (P3: an error-type leak).
    - A fresh Opus fixer takes all 7 in one round.
    - **PM decision pending user review:** META types only the YAML 1.2 core `true`/`false` and decimal numbers. `yes`, `no`, `on`, `off`, dates and leading-zero numbers stay strings, exactly as written.
  - **T-006 race follow-up: running** (Sonnet builds, Codex reviews), worktree `T-006-race`.
  - **T-008 (K1 embeddings): Gemini scout running**, task file drafted in worktree `T-008`. The code and CPU tests can be built now; the live GPU smoke waits on question 2.
  - **T-007 (K0 sync): accepted and pushed.**
    - The Sonnet re-review passed, and so did its check of fix round 2.
    - Codex combined T-007 with T-009. The test profile is now built from `enhancer.example.yaml` plus a shared `tests/conftest.py` helper.
    - Main pipeline suite: `1824 passed, 31 skipped`. A Pillow deprecation warning remains: saving `I` mode PNGs, at `model_image.py:80`, is a T-009 follow-up.
    - The check found a Windows-only race in committed T-006 storage (P2, fails closed). A reader's `release()` unlinks its lease without the lock, while GC resolves the lease names, and `ntpath.realpath` of a vanished file keeps `\\?\`, which reads as "outside project root". It is fixed in the T-006 race follow-up.
  - **T-022 (CI):** the first review's F1 to F3 are closed ([status-history](archive/status-history-2026-11-14.md)). The same retest FAILED on RT1, a P1: no check of the wiki, releases, Actions runs or artifacts before going public. It also FAILED on RT2, a P2: a signal during the last private recheck still lets the public edit through. Fix round 2 (Opus) closed both: 234 ci-window tests pass and 20/20 mutants are killed. The Codex retest 2 is running. No CI window opens until a retest passes.
  - **T-026 (quota): accepted and pushed.**
    - The round 4 retest (decision 114) passed RT10 to RT12.
    - The Sonnet fix for RT14 puts the diagnostic first, never truncates it, and drops whole entries with `+N more`.
    - The Codex recheck then PASSED: 645 quota tests, 144 probes.
    - Main tooling suite with an empty `AGENT_WORKFLOW_STATE_DIR`: `26 failed, 5412 passed`. The 26 are tests that expect the default state dir; they give `29 passed` without the override. This is the same isolation defect as the 42 live-ledger failures (S42 queue). `PM_POLICY.md` was trimmed to its budget, and its ordinary-review row now makes Gemini the first reviewer, never the only one.
  - **S42 Antigravity runner: accepted and pushed.**
    - Round 2 closed RF1 and RF2.
    - Round 3 wraps every collection read in `@(...)` after a class audit, which closed R2F1 (a scalar CIM `Count` in `undo.ps1`).
    - The Codex retest 3 PASSED; `test_agy_lib.py`: 149 passed.
    - Its P3s are in the S42 queue.
    - Before you run `setup.ps1`, see question 6.
  - **S42 PM fix:** `AGENT_BRIEF_FOOTER.md` item 11 now says "Reply in English: your reader is the PM, not the user", because workers had taken the PM for the user under AGENTS.md section 1. 191 related tests passed. Another family still has to review it.
  - **S42 queue:**
    - `brief_lint` false positive: task-file text such as "every commit reachable" is flagged as a git write;
    - `gemini-ws.ps1` reads files fail-open;
    - `tool_guard` should block a PM Bash `cd` below the project root;
    - the ledger false positive when another worktree is removed;
    - Codex temp dirs that cannot be removed;
    - the runner's R3F1 (the AST lint misses `$x = (Get-X)` and member targets) and R3F2 (the audit's false claim that a scalar PSCustomObject has `Count`);
    - the pre-push check misses the machine's own absolute root in tracked files (lesson 2026-11-15);
    - `test_cli_worker_codex.py` and `test_codex_hooks.py` read the live `.tmp/pm-state` ledger, not a test state dir: in main, 42 tests fail with `git_history_changed`. They pass on a clean HEAD, and they pass in main with `AGENT_WORKFLOW_STATE_DIR` set to an empty folder (2026-11-15 18:4x). The override itself breaks 26 other tests (`test_brief_lint` send, `test_pm_supervisor`, a `test_session_context` test), which expect the default state dir: no single setting runs the whole suite green. Fix: each test gets its own state dir.
  - **Follow-ups:** T-006 storage gets lease reclaim by owner identity, deferred from T-007; the handover from `pin()` to the caller is a documented known limit.
  - **PM decisions pending user review (2026-11-15):**
    - **T-022 RT1 rule:** the wiki is off, else refuse; no releases, issues, pull requests, discussions or Pages may exist; Actions runs and artifacts are allowed only from run IDs this tool dispatched and recorded; any unknown, error or truncated answer refuses; GitHub content is never deleted. Those run IDs are kept in `runs/ci-window/ledger.json` (key `run_ledger`); a failed ledger write is a window error. The gate blocks every later window while an unknown run is retained. The user hears this before the first CI window.
    - **T-022:** gitleaks baseline matched by gitleaks' own fingerprint `commit:file:rule:line`, because `--baseline-path` did not suppress on a real run. A Windows console close counts as a hard kill. After an interrupt the run is cancelled only once the repository is private again. The version check gets a `version_args` key.
    - **T-009:** a required `anthropic_workspace_id` (an ID or `none`). A required alpha policy, with the example `flatten #FFFFFF`. A credential file with more than one hard link is refused. `Authorization: Bearer` is kept, as documented primary.
    - **T-007:** `data_root` is in the `kb sync` read set and may not overlap the knowledge base. Validation runs before any data is opened or any lock is taken. Storage ownership edits are allowed. A linked root is resolved at start. NEL and U+2028 are data. A file named only by an extension is counted as ignored. Lease reclaim is deferred.
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
- **Earlier plan, with target finish times (decision 95; local time 2026-11-15 from `Get-Date`, refreshed 14:00).** Basis, measured today: fix rounds took 35 to 60 min, Codex retests 15 to 40 min, Gemini reviews 5 to 20 min, a Codex build with 3 fix rounds about 3 h.
  - **T-024, T-006, and the earlier T-009 build history:** moved verbatim to [status-history-2026-11-14](archive/status-history-2026-11-14.md). T-006 residual follow-up: `DataStore.read` lacks an internal-link test. Deployment note: `storage.lock.timeout_s` grows with snapshot size.
  - **T-009 PM decision pending user review (2026-11-15):** the contract requires the engine and its version in every call record and request hash (`contracts/vlm-request.md:39,61`) but no key held them, so `vlm.backends.<b>.engine: {name, version}` is added: for `openai_compatible`, `name` is `vllm` or `llama_cpp` and `version` is `pin_required`; for a cloud backend, the name is the provider and the version is the API version the profile already pins. The request hash covers the configured settings before any unsupported field is dropped. The T-009 decisions the user accepted (decision 106) are in [status-history](archive/status-history-2026-11-14.md).
  - **Closed S42 fixes (stdin briefs, contract-drift docs):** in [status-history](archive/status-history-2026-11-14.md). The Gemini implementation smoke waits on question 6.
  - **Pipeline first (decision 100; the user asked on 2026-11-15 why tooling took so much time):** after T-024 lands, new tooling work waits behind the pipeline queue. Only defects that block pipeline work are fixed at once (S42).
  - **Pipeline queue:** T-008 next (scout running); T-010 waits on T-008 and E12.
  - **Tooling in parallel, second priority (decision 105):** T-026 (quota) and T-021 (brief order and scope guard) run beside the pipeline queue, so agents stay easy to manage. A pipeline task gets a free worker or the Gemini slot first. T-026 starts once T-024 is integrated (they share `settings.example.json` and the SessionStart hook, decision 97): phase 1 (Claude Opus) built at 13:50 (4803 passed). The Codex review failed it (4 P1, 5 P2); fix round 1 closed those (4875 passed). The retest left 4 (malformed Codex window slots, future timestamps, an old reading clearing a newer block, an unlocked PM write); fix round 2 of 3 is running. Phase 1 accepted about 17:00, phase 2 about 19:00. Pre-approved: the one-shot wake dry run (decision 108) and the PM adding the Antigravity status line to the user's settings (decision 109). T-021 follows T-026 (same hooks); target about 20:00.

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
2. **E12 download (asked 2026-11-15 18:45):** may the PM download the dev model files and a llama.cpp `llama-server` build into the project? The files are decision 20's `unsloth/Qwen3.5-4B-GGUF` `Qwen3.5-4B-Q3_K_M.gguf` (2.29 GB) and `mmproj-F16.gguf` (0.67 GB), into `models/` with a manifest. The build goes into `.tmp/tools/llama.cpp/<release>/`, pinned by release and SHA-256 like gitleaks (decision 113). Recommendation: yes. E12 needs these, T-010 and T-014 need the dev server, and nothing outside the project changes.
3. Before any paid cloud call, the PM asks for the number of calls allowed and the key file in `.secrets/`.
4. Targets for linking coverage (E1), latency on the deployment (E13) and card precision (E16): the user sets them after the first measurements (decision 34); the PM brings the numbers.
5. Open questions from the cold read: listed in the T-004 task file ([archive/T-004-cold-read.md](archive/T-004-cold-read.md)).
6. **Antigravity runner setup (asked 2026-11-15):** Gemini implementation jobs need `scripts/agy-runner/setup.ps1`, run by you in an elevated PowerShell (steps in [scripts/agy-runner/README.md](../scripts/agy-runner/README.md), "Set up the runner"). On this machine the account `agy-runner` has explicit Deny rules on the projects folder, this project root and `.tmp\agy-work`, so setup stops at the root. Clearing them without lifting the `.secrets` Deny is your decision. Recommendation: later. Read-only Gemini reviews and scouts work today without the runner.