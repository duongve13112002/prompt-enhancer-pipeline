# STATUS history, 2026-11-14 (moved verbatim from tasks/STATUS.md on 2026-11-15)

- **Session 2026-11-14 (kickoff, decisions 54 to 63):** agent environment set up on the Windows dev machine (`setup_project.py check`: 20 ok, 0 warn, 0 fail; Python 3.12.10; claude 2.1.323, codex-cli 0.169.0, agy 1.5.8); repository confirmed private. `database/` is the dev knowledge base (43 folders, 47 concepts), Git-ignored. Decisions 59 and 60 changed the info-file contract (a model assigns line-2 text that does not split; only the info file and image extensions are read). Cold read round 2 (R-028, [task](../active/T-004-cold-read-round2.md)): done 2026-11-14 by Haiku, Sonnet and Opus 5.5 readers; their text defects are fixed and their questions sent to the user.
- **T-002 phase 1 accepted 2026-11-14:** `tools/env_report.py` (read-only inventory; required `--data-root`, `--scratch`, `--models-root`, `--timeout-seconds`; GPU only with `--gpu`) and `tools/tests/test_env_report.py` (7 passed), written by Codex, reviewed by two fresh Codex reviewers and a Claude Sonnet reviewer (findings F1 to F4 and a P2 fixed). Facts: Windows 11, Python 3.12.10, RTX 5070 Laptop GPU 8151 MiB, driver 616.92, CUDA UMD 13.4; physical core count unknown (CIM access denied); Linux not run. Report kept at `.tmp/T-002/environment-report.md` (Git-ignored). **Phase 2 done 2026-11-14:** on Windows the 10 Linux-container failures do not occur. The PM's unsandboxed full run gave 3157 passed, 4 skipped, 1 failed. Inside the Codex sandbox the run gave 3094 passed, 7 failed, 5 errors: 11 are sandbox effects (the stat of `.secrets/README.md` is denied, process listing is empty, child processes outlive `kill_tree`), and 1 is a real timing defect in `test_pm_invariants.py::test_read_of_the_result_silences_it_until_it_changes` (`NOW` is fixed at import while the read marker uses the wall clock), fixed by the PM under S42 (another family reviews it). The 11 sandbox failures pass in the PM's unsandboxed run. **T-003:** inventory done; a hand-written silver sample of 55 scenarios passes its checks and the user accepted it, asking for harder scenarios where useful (decision 65, [task file](../active/T-003-evaluation-set.md)); key check by another family done (Gemini 3.8 Flash medium, 55 of 55 agreed; edit images not checked). The user deleted folder `Luc_quan_Mùa_hè` (decision 64): 42 folders, 46 concepts. The kickoff changes were committed and pushed by the orchestrator as a one-off (decision 66).
- **Later on 2026-11-14 (decisions 67 to 73; 73: an edit's JSON describes the whole edited image; committed and pushed by the orchestrator as a one-off, decision 74):** output language in the manifest, default `English`; `clarification.mode: best_effort`; context never vetoes the user's concept; the user's admin prompt `v1` (one prompt for generate and edit; ratio and bbox ignored) replaces the example prompt; scale unknown and possibly large; the orchestrator fixes agent-tooling defects itself. Agent smoke tests: Codex works through `cli_worker.py`; Antigravity works through `gemini-ws.ps1` and `gemini-review.ps1` after three fixes (relative tar path, `_results` created, session allow-rule `read_file(<workspace>)`; `test_agy_lib.py` 55 passed); Claude workers work through `cli_worker.py` (the cold-read readers).

## Moved 2026-11-15 18:09 (closed rows of the 2026-11-15 plan)

  - **T-024**, tooling: accepted and pushed at 12:43 (commit `d5bc788`).
  - **T-006** storage, Claude Opus builds: fix rounds 1 and 2 done (953 passed). The third Codex review closed classes R1 (links out of the store) and R3 (re-entered Writer) and left 2 P1 in R2: Windows path aliases bypass artifact protection, and a commit check and its replacement are not serialized. A Gemini class audit found 7 more of the same family. Round 3 closed R2 and every Gemini lead; the fourth Codex review left one P1 (an internal junction let a write overwrite a published artifact). The Fable 5.2 round of decision 107 closed it at 15:00 (every single-path operation refuses a link at any component; 72 class cases). **Accepted 15:30:** Gemini and Codex both passed it, and the main-tree pipeline suite gave 1154 passed, 26 skipped. Residuals: another program swapping a store folder for a link during an operation is outside the store's contract, and file-symlink cases for `CURRENT`, `LOCK` and `WRITER` are skipped on this machine (Linux CI covers them). Gemini noted that `DataStore.read` lacks an internal-link test; that is follow-up work. Deployment note: publish re-hashes the snapshot under the lock, so `storage.lock.timeout_s` grows with snapshot size.
  - **T-009** model client, Codex builds: two workers were retired near their context limit; the third, Codex `high`, built the function that makes the client from a loaded profile, the effect tests, a per-image decode limit that does not touch Pillow's process-wide setting, and `output.validate_response` (1075 passed). Left: engine name and version in the call record and request hash. Implementation about 15:45, Gemini then Claude review about 16:45, accepted about 17:15; E12 on the GPU after review.

## Moved 2026-11-15 19:31 (accepted pipeline task T-009)

  - **T-009 (model client): accepted and pushed (`6b03510`).** Reviews: Gemini PASS; Claude Sonnet PASS after two Codex fix rounds. The main suite gave 1686 passed, 30 skipped.
    - Residual P3s: `call()` maps credential and config `ValueError`s to a generic `INPUT_INVALID` message, though the cause is chained; the 32 incompatible image cells are pinned to Pillow 11.3.0.
    - `not_run`: real symlink tests (no link privilege here; junctions and hard links were tested), Python 3.11, ruff, Linux.
  - **Pipeline queue:** the T-007 (K0 sync) and T-012 (profiles) scout notes are written (`.tmp/T-007/research.md`, `.tmp/T-012/research.md`). T-007 starts when T-006 is accepted (about 16:15), T-012 when T-009 is accepted (about 17:15); T-008 (K1 embeddings, dev GPU) follows T-007.
  - **T-022 (CI):** the Codex review FAILED with 2 P1 and 1 P2:
    - a Ctrl+C at cleanup entry leaves the repository public;
    - remote refs are re-checked only after the repository is public, and only `main`;
    - the scanner version is never checked.
    - Fix round 1 closed F1 to F3, as the Codex retest confirmed.
  - **S42 fix (2026-11-15):** Claude workers now get their message on stdin, not argv (a 56 KB brief hit the Windows 32767-character command line); Codex reviewed it.
  - **Contract-drift docs fix:** done and pushed (`a2319dc`) from a read-only Gemini audit. The Gemini implementation smoke is blocked: `.tmp/agy-jobs` is missing, and only the user can recreate it (`scripts/agy-runner/setup.ps1` as administrator). Purge now keeps both runner folders.
  - **T-009 PM decisions, accepted by the user (decision 106):** schema minimums close values the client refuses (`max_concurrency` >= 1, non-empty `base_urls`, routes, `model.id`, `model.revision`); provider-dependent rules are checked in `load_config` for each role's bound backend only (Anthropic `budget_tokens` >= 1024 and < `max_tokens`); `provider_default` resolves to the provider's documented base URL or is refused; absent image members are refused except the decision 84 fallback; keys this client does not read are recorded as owned by their task; `vision.quality` is read by the shared resize of T-009, and `vision.transport` belongs to T-008.

## Moved 2026-11-15 19:45 (accepted T-026)

  - **T-026 (quota): accepted and pushed.**
    - The round 4 retest (decision 114) passed RT10 to RT12.
    - The Sonnet fix for RT14 puts the diagnostic first, never truncates it, and drops whole entries with `+N more`.
    - The Codex recheck then PASSED: 645 quota tests, 144 probes.
    - Main tooling suite with an empty `AGENT_WORKFLOW_STATE_DIR`: `26 failed, 5412 passed`. The 26 are tests that expect the default state dir; they give `29 passed` without the override. This is the same isolation defect as the 42 live-ledger failures (S42 queue). `PM_POLICY.md` was trimmed to its budget, and its ordinary-review row now makes Gemini the first reviewer, never the only one.

## Moved 2026-11-15 19:00 (accepted tooling tasks)

- **T-025 accepted 2026-11-14:** `purge_tmp.py` gets `sweep` and `prune-archive`, `--dry-run` on every deleting command, and one gate (`Gate`) for all of them. The gate refuses protected trees; trees a busy worker, a task in flight or an unfinished Antigravity job owns; manifest-bound files of every root that can bind the target; and any path with a link on an ancestor. Ages live in `tools/agent-workflow/cleanup_config.json`, and the SessionStart line reports the `.tmp` size and the orphan count. Sonnet 5.5 implemented it in 3 fix rounds (the cap); the Codex review found 4 P1 and 1 P2, then 2 P1, then 1 P1, all fixed, and the final retest PASSES. Accepted residual risk: the Windows time-of-check link race.
- **T-024 accepted 2026-11-15:** no agent commits, and the copied tooling defects fixed ([task](../active/T-024-tooling-no-agent-commits.md)). After six fix rounds and a final round of five file-disjoint packages (A to E), the Codex review found 6 findings (CX1 to CX6). One review fix round closed them all, and the Codex retest 6 confirmed each one by a named test and a replayed probe. The PM's S42 fix to `setup_project.py` (the checkout's own path exempted as a whole path component only) passed the same retest. Full tooling suite on the combined tree: 4757 passed, 9 skipped. Accepted residuals: decision 89. The user accepted the PM decisions (decisions 103 and 104). Row history, moved verbatim: [archive/T-024-status-history.md](T-024-status-history.md).
- **T-020 accepted 2026-11-15 (decision 97):** the class test sets of `brief_lint.py` come from the validated `tools/agent-workflow/brief_lint_config.json`, and any failure to load or apply it blocks, even behind a waiver ([task](../active/T-020-brief-lint-test-sets.md)). Sonnet 5.5 implemented it. The Codex review found 2 P2, fixed in one round; the Codex retest PASSED. Full suite in main: 3237 passed, 4 skipped. Two separate items:
  - `cli_worker.lint_gate` allows a brief when the lint itself crashes; T-024 package D is fixing this.
  - A configured regular expression has no execution deadline; this is accepted for now, because the PM writes the configuration.

## Moved 2026-11-15 18:45 (closed history rows of "Current state")

- **Tooling tasks started (decision 75):** T-024 ([task](../active/T-024-tooling-no-agent-commits.md), Claude Opus 5.5 high) and T-025 ([task](../active/T-025-cleanup-tooling.md), Codex) in parallel, then T-026 ([task](../active/T-026-quota-tooling.md), Codex). Dispatched 2026-11-14 ~22:00 local: T-024 Step 0 approved by the user (decision 78), implementing; T-025 Step 0 approved by the PM, moved from Codex to Claude Sonnet 5.5 because the Codex guard protects `tools/agent-workflow/` (T-026 moves to Claude too), implementing.
- **Quota at 2026-11-14 21:45 local:** all three providers' weekly windows free (decision 76).
- **Tooling check at seeding:** `python3 -B -I tools/agent-workflow/setup_project.py check` in a Linux container: binding, instruction files, budgets, hook-read files, Git-ignore entries and foreign tokens OK; warnings for machine facts (no `.venv`, no Codex or agy CLI, no PowerShell, hooks not registered, no `.tmp/pm-state`); `setup_project.py apply` was not run because it renders machine-local settings.
- **Not run:** nothing was implemented, measured or benchmarked. No live Claude, Codex or agy worker was started in this layout; hooks were not exercised in an interactive session. **Tool tests (2026-11-13, Linux container, Python 3, pytest in a scratch venv):** 3017 passed, 82 skipped, 10 failed; the same 10 fail on the earlier copy of this repository and the checked ones fail in the user's original project too (Windows-specific behaviour: process trees, PID times, links, path separators, PowerShell commands), so they are to be rerun on the Windows dev machine (task T-002 reports the result).

## Moved 2026-11-15 20:02 (accepted pipeline task T-005)

- **T-005 accepted 2026-11-15:** the configuration and profile loader (`src/prompt_enhancer/config.py`, `tests/`; [task file](active/T-005-profile-loader.md)). Codex implemented it in 3 fix rounds (the cap). Sonnet 5.5 reviewed each round and Gemini 3.8 Flash reviewed round 2; the final Sonnet retest PASSES. Pipeline suite: 804 passed, 25 skipped. Not run: Linux, ruff, a real 8.3 short-name alias (short names are off on this volume). One open P3: `resolve_writer_format` names a bare file name, not the full path. **PM decisions from the fix rounds, accepted by the user (decision 103):**
  - Configured roots resolve inside the project, are not the project root itself, and lie outside `.secrets`, `.git`, `.venv` and `models`, compared after resolving links.
  - `api_key_file` is `none` or literally `.secrets/<name>`, case exact on every OS, and is never opened.
  - Trailing dots or spaces, surrounding whitespace and Windows device names are refused on every OS.
  - Relative `load_config` and `load_profile` arguments are project-root-relative.
  - The overlay may set `max_concurrency`.
- **T-005 build (decision 95):** a Haiku scout wrote the research note, then Codex implemented (Step 0 first) and Claude reviewed. **PM decisions, accepted by the user (decision 104):** the string `none` has one meaning everywhere, "this optional setting is off / not sent" (as in decision 84), so schemas that reject it for `seed`, `stop` and `budget_tokens` accept it; a whole-configuration schema `enhancer_config.v0` is written from the documents, including `clarification`; `unsupported_param_policy` is required, and the examples set `warn` (warn-and-continue, T-009); each command has an explicit read-set table and an unknown command is refused; overlays merge recursively (scalars and lists replaced, cycles refused); the package lives in `src/prompt_enhancer/` with tests under `tests/`; an overlay inherits non-deployment values such as `unsupported_param_policy` from its base; documented settings that have no key yet (K1 embedding sizes and devices, S0 deadlines) are listed in `contracts/README.md` as owned by T-008 and T-013; the writer's `format: from_profile_manifest` resolves to `json_schema` when the manifest names a schema and to `none` otherwise (`docs/stages/S4-writer.md:15`).

## Moved 2026-11-15 20:35 (closed T-026 timeline)

  - **Tooling in parallel, second priority (decision 105):** T-026 (quota) and T-021 (brief order and scope guard) run beside the pipeline queue, so agents stay easy to manage. A pipeline task gets a free worker or the Gemini slot first. T-026 starts once T-024 is integrated (they share `settings.example.json` and the SessionStart hook, decision 97): phase 1 (Claude Opus) built at 13:50 (4803 passed). The Codex review failed it (4 P1, 5 P2); fix round 1 closed those (4875 passed). The retest left 4 (malformed Codex window slots, future timestamps, an old reading clearing a newer block, an unlocked PM write); fix round 2 of 3 is running. Phase 1 accepted about 17:00, phase 2 about 19:00. Pre-approved: the one-shot wake dry run (decision 108) and the PM adding the Antigravity status line to the user's settings (decision 109). T-021 follows T-026 (same hooks); target about 20:00.

## Moved 2026-11-15 20:47 (T-009 engine key decision, accepted by decision 119)

  - **T-009 PM decision pending user review (2026-11-15):** the contract requires the engine and its version in every call record and request hash (`contracts/vlm-request.md:39,61`) but no key held them, so `vlm.backends.<b>.engine: {name, version}` is added: for `openai_compatible`, `name` is `vllm` or `llama_cpp` and `version` is `pin_required`; for a cloud backend, the name is the provider and the version is the API version the profile already pins. The request hash covers the configured settings before any unsupported field is dropped. The T-009 decisions the user accepted (decision 106) are in [status-history](archive/status-history-2026-11-14.md).

## Moved 2026-11-15 20:55 (answered question 2)

2. **Answered 2026-11-15 20:1x: yes (decision 115); the PM is downloading.** Asked at 18:45, **E12 download:** may the PM download the dev model files and a llama.cpp `llama-server` build into the project? The files are decision 20's `unsloth/Qwen3.5-4B-GGUF` `Qwen3.5-4B-Q3_K_M.gguf` (2.29 GB) and `mmproj-F16.gguf` (0.67 GB), into `models/` with a manifest. The build goes into `.tmp/tools/llama.cpp/<release>/`, pinned by release and SHA-256 like gitleaks (decision 113). Recommendation: yes. E12 needs these, T-010 and T-014 need the dev server, and nothing outside the project changes.

## Moved 2026-11-15 21:05 (answered question 6, earlier text)

6. Asked earlier the same day, **Antigravity runner setup:** Gemini implementation jobs need `scripts/agy-runner/setup.ps1`, run by you in an elevated PowerShell (steps in [scripts/agy-runner/README.md](../scripts/agy-runner/README.md), "Set up the runner"). On this machine the account `agy-runner` has explicit Deny rules on the projects folder, this project root and `.tmp\agy-work`, so setup stops at the root. Clearing them without lifting the `.secrets` Deny is your decision. Recommendation: later. Read-only Gemini reviews and scouts work today without the runner.

## Moved 2026-11-15 22:00 (T-022 accepted, T-007 race note, S42 test_slot item, answered question 7)

  - **T-022 (CI): accepted 2026-11-15 21:50 after fix round 4** (reviews: retest 1 RT1 P1 and RT2 P2, retest 2 three ledger P2, retest 3 R3F1, all closed). Round 4 holds every trusted folder open and checks containment on the handle (`tools/agent-workflow/trusted_state.py`). Retest 4: Sonnet PASS (273 passed; real rename and junction probes all blocked; a torn ledger fails closed) and Gemini PASS. **Follow-ups (P3):** F1 a hard-linked ledger is not refused (`trusted_state.py:238`, `st_nlink`); F2 the whole chain from the root stays held for up to 300 s, blocking renames in the root and `.tmp` during a scan (hold only the leaf); F3 `docs/ci.md:66` still names `os.replace`. Accepted residual: a signal after the final cancellation check; the always-run restore makes the repository private again.
    - The check found a Windows-only race in committed T-006 storage (P2, fails closed). A reader's `release()` unlinks its lease without the lock, while GC resolves the lease names, and `ntpath.realpath` of a vanished file keeps `\\?\`, which reads as "outside project root". It is fixed in the T-006 race follow-up.
    - `test_slot.py` should start its child with `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)`, which the child's own children inherit, so that a native crash is logged instead of opening a modal "Application Error" dialog. One opened at 20:27 for a crashed child of the T-012 retest probe (process chain 35564 -> 20968 -> 26728), and its run lost the result;
7. **T-022 fix-round cap: answered 2026-11-15, two more rounds (decision 120).** Fix round 4 is dispatched.

## T-012 history (moved 2026-11-15 23:1x)

  - **T-012 (P profiles): fix round 1 done at 19:58, with a part b running.** Built by Opus: 84 P tests, 22/22 mutants. Both reviews FAILED. The Codex review found 1 P1 and 4 P2 (check races, a manifest overwrite, META coercion, a config drift, an unverified results hash). Gemini added 1 P2 and 1 P3.
    - A fresh Opus fixed all 7, plus one more in the same class: A7, where `confirm_profile` now refuses a changed profile with `PROFILE_CHANGED`. That gave 135 P tests, 10/10 mutants, and a pipeline suite of 1821 passed.
    - F1 locks `check` with the existing `storage.lock.timeout_s`/`poll_s` keys. The PM accepted its matching change to the T-005 test `test_real_loader_examples`.
    - Part b closed both leftovers: the manifest `meta` and the golden-input hash. The P tests passed (152).
    - The Codex retest at 20:3x FAILED on 4 P2:
      - RT1: a YAML alias of the META node is deleted at the split;
      - RT2: the writer turns U+0085 into a space;
      - RT3: an edit made during serialization is lost on confirm;
      - RT4: activation does not bind the envelope schema.
    - Fix round 2 (done 22:4x) closed RT1 to RT4 and the audit's extra rows (recursion and overflow leaks, recursive aliases, comments lost on confirm, a final check that missed golden files): 40 regressions, 10/10 mutants, P tests 192 passed. Retest 2: Gemini PASS (0 findings) and Sonnet PASS (193 P tests; 7 P3). **Accepted 23:2x;** applied to main. P3 follow-ups: R2-1 a 100000-bracket `activation.json` leaks `RecursionError` (`profiles.py:645`, fails closed); R2-2 the lost-edit window is one fsync wide; R2-3 confirm refuses a CRLF or BOM manifest; R2-4 a leftover `check.lock` for an unknown id; R2-6 no test of the addendum clause; R2-7 register can lose a paid draft when the lock is busy after the model call. **PM decision pending user review (R2-5):** `check_code_sha256` hashes all of `config.py`, so any edit there, comments included, forces a new paid golden run; kept fail-closed until you choose a narrower scope. Residual: an external edit between confirm's comparison and its replace is lost (documented). **PM decisions pending user review:** a code fingerprint and schema hashes in activation; raw NEL, LS and PS refused in admin YAML; confirm rewrites only its own writer form; one bundle lock for register and confirm (`PROFILE_BUSY`).

## Accepted PM decisions of 2026-11-15, decision 119 (moved 2026-11-15 23:1x)

    - **T-022 RT1 rule (accepted by the user, decision 117):** the wiki is off, else refuse; no releases, issues, pull requests, discussions or Pages may exist; Actions runs and artifacts are allowed only from run IDs this tool dispatched and recorded; any unknown, error or truncated answer refuses; GitHub content is never deleted. Those run IDs are kept in `runs/ci-window/ledger.json` (key `run_ledger`); a failed ledger write is a window error. The gate blocks every later window while an unknown run is retained. The user hears this before the first CI window.
    - **T-022:** gitleaks baseline matched by gitleaks' own fingerprint `commit:file:rule:line`, because `--baseline-path` did not suppress on a real run. A Windows console close counts as a hard kill. After an interrupt the run is cancelled only once the repository is private again. The version check gets a `version_args` key.
    - **T-009:** a required `anthropic_workspace_id` (an ID or `none`). A required alpha policy, with the example `flatten #FFFFFF`. A credential file with more than one hard link is refused. `Authorization: Bearer` is kept, as documented primary.
    - **T-007:** `data_root` is in the `kb sync` read set and may not overlap the knowledge base. Validation runs before any data is opened or any lock is taken. Storage ownership edits are allowed. A linked root is resolved at start. NEL and U+2028 are data. A file named only by an extension is counted as ignored. Lease reclaim is deferred.

## T-006 race follow-up and T-006-int history (moved 2026-11-16 00:1x)

  - **T-006 race follow-up: fix round 2 (store lock on release, decision 123).** Review FAILED F1 (P1, `readlink` failure taken as "not a link"), F2 (P2, tolerated `StorageError` hid link refusals), F3 (P3, no NTFS evidence). Fix round 1 closed F1 and F3; retest 1 FAILED R1 (P2): a late junction swap is still classed as a tolerated "path vanished" (`config.py:132-164`, `store.py:300-575`). Round 2 removed that tolerance and added anchored removal for lease and snapshot entries: Sonnet retest 2 and Gemini both PASS that delta (R1, N1, N2, N4 closed). Both name the same open class (P1, pre-existing): registry, hold, state and artifact deletes and overwrites still act by name, and real junction probes deleted or overwrote outside files. Round 3 (21:59, fresh Opus) routes every store mutation through the anchored layer, keeps GC going past a damaged manifest it does not need, and stops `release()` in a `finally` from masking the caller's result. **PM decision pending user review:** round 3 covers the whole class rather than accepting the P1 residual; no repair command this round. Round 3 done 23:0x: every store mutation goes through `_anchored.py` (renames via `NtSetInformationFile`), name-based helpers removed with an AST guard, 13 regressions, 10/10 mutants, full suite 0 failures. Retest 3 PASSED (Sonnet: no escape, 4 P3; Gemini: PASS); the CI red `test_link_resolved_containment` is over-strict, not an escape. Integration FAILED to collect: T-012's `profiles.py` imports the removed `_temporary`/`fsync_dir`/path `atomic_write_bytes`. Main kept at 350df5e; integration task T-006-int (23:1x, fresh Opus, worktree at 350df5e plus the race patch) moves `profiles.py` onto the anchored layer, extends the AST guard to all of `src/`, fixes the link test and the P3s. Round 1 done 23:51: every file mutation in `src/` goes through `_anchored.py` (profile bundles anchored at the project root, `publish_new_bytes` no-replace), AST guard over 24 modules, 7/7 real-junction escapes before and refused after, 10/10 mutants, 2194 passed. **PM decision pending user review:** profile folder names that fail `check_segment` are refused. Reviews by Sonnet and Gemini running. **PM decision pending user review:** a failed `release()` becomes a note on the caller's exception, or a `StorageWarning` when the work succeeded.

## 2026-11-16 T-008 round history (moved from STATUS at acceptance)

  - **T-008 (K1 embeddings): fix round 1 done (20:58).** The Codex review FAILED 3 P2 (numeric overflow without a failed checkpoint, a sentinel `embedding.candidates` accepted, no exact metadata property). The fix closed all three (2016 passed). Gemini PASSED it; the Codex retest FAILED on 3 new P2 (a BigTIFF offset overflow aborts K1, TIFF orientations 5-8 store oriented sizes, an unbounded `embedding.dimension` crashes Arrow) and 1 P3. Fix round 2 closed RT1 to RT4 and an audit find (`vision.quality` unbounded in the standalone preflight); 9/9 mutants; full suite 2045 passed, 1 intermittent T-006 storage failure. Gemini PASSED round 2 (P3: `embedding.batch_size` lower bound); the Codex retest 2 FAILED on 2 P2: a truncated QOI raises `IndexError` past the decode boundary, and the standalone preflight skips the schema's numeric domains (`batch_size: -1` returns an empty result). Round 3 put one boundary around every decode of file bytes and reused the loader's validation on the standalone path; Gemini PASSED it (0 findings); the Codex retest 3 (23:04) closed R2F1/R2F2 and FAILED on 2 P2 of one wider class: `vision.format` empty or unknown passes (fallback sends `data:image/;base64`, unknown encoder aborts after the read), and an integer timer `10**400` passes both paths then overflows AnyIO's deadline. Round 4 (fresh Opus, decision 121, done 23:34) audited all 35 keys K1 reads: `vision.format` is refused by name against Pillow's encoder registry before any read, and every schema number (timers and backoffs) must convert to a finite float; 2431 passed, 9/10 mutants. **PM decision pending user review:** the loader applies that timer rule to every backend, not only K1's. Gemini PASSED round 4 (2 P3); the Codex retest 4 FAILED R4F1 (P2: data URI media type built from the encoder name, `image/jpeg2000` for JPEG2000 where Pillow registers `image/jp2`) and reproduced N1 (P2, inherited from T-009: `model_transport.py:65` applies the backoff cap after the power, so 1025 retries with zero backoff abort K1 with no checkpoint). Round 5, the last allowed (decision 121), started 23:5x with a fresh Opus: one registry-derived media type for K1 and T-009 and overflow-safe capped backoff. A failed retest 5 blocks the task and goes to you.
  - **Pipeline queue:** T-008 next (scout running); T-010 waits on T-008 and E12.

## Rows moved from STATUS 2026-11-16 06:0x (T-007, S42 runner, PM decisions accepted by decision 119)

  - **T-007 (K0 sync): accepted and pushed.**
    - The Sonnet re-review passed, and so did its check of fix round 2.
    - Codex combined T-007 with T-009. The test profile is now built from `enhancer.example.yaml` plus a shared `tests/conftest.py` helper.
    - Main pipeline suite: `1824 passed, 31 skipped`. A Pillow deprecation warning remains: saving `I` mode PNGs, at `model_image.py:80`, is a T-009 follow-up.
  - **S42 Antigravity runner: accepted and pushed.**
    - Round 2 closed RF1 and RF2.
    - Round 3 wraps every collection read in `@(...)` after a class audit, which closed R2F1 (a scalar CIM `Count` in `undo.ps1`).
    - The Codex retest 3 PASSED; `test_agy_lib.py`: 149 passed.
    - Its P3s are in the S42 queue.
    - Before you run `setup.ps1`, see question 6.
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

## Rows moved from STATUS 2026-11-16 06:0x (T-012 and T-008 accepted rows)

  - **T-012 (P profiles): accepted 2026-11-15 23:1x** (Opus build, 2 fix rounds; Codex, Gemini and Sonnet reviews; history in the status archive). Full pipeline suite on main: 2017 passed, 31 skipped.
    - P3 follow-ups: R2-1 a 100000-bracket `activation.json` leaks `RecursionError` (`profiles.py:645`, fails closed); R2-2 the lost-edit window is one fsync wide; R2-3 confirm refuses a CRLF or BOM manifest; R2-4 a leftover `check.lock` for an unknown id; R2-6 no test of the addendum clause; R2-7 register can lose a paid draft when the lock is busy after the model call. **PM decision pending user review (R2-5):** `check_code_sha256` hashes all of `config.py`, so any edit there, comments included, forces a new paid golden run; kept fail-closed until you choose a narrower scope. **PM decisions pending user review:** a code fingerprint and schema hashes in activation; raw NEL, LS and PS refused in admin YAML; confirm rewrites only its own writer form; one bundle lock for register and confirm (`PROFILE_BUSY`).
    - **PM decision pending user review:** META types only the YAML 1.2 core `true`/`false` and decimal numbers. `yes`, `no`, `on`, `off`, dates and leading-zero numbers stay strings, exactly as written.
  - **T-008 (K1 embeddings): accepted 2026-11-16 00:46** (Opus build, 5 fix rounds under decision 121; Codex retest 5 PASS, Gemini PASS; round history in the status archive). Main suite with T-008: 2900 passed, 42 skipped. Integration: `config.py` import merged by hand; K1's `_commit` renamed `_commit_record` (the anchored-writes AST guard refuses the removed helper's name). Follow-ups: N3 P2 (T-009) ICO under 16 px gives an empty container; N4 P3 MPO labelled `image/mpo` with JPEG bytes; N5 P3 PALM and XBM cannot encode RGB (refused as `encode_failed`); N2 P3 integral floats outside K1's read set. **PM decision pending user review:** the loader applies the finite-float timer rule to every backend.
    - **PM decisions pending user review:**
      - The embedder is a named `vlm.backends` entry.
      - There is no `embedding.devices` key: the server launch places the model.
      - The llama.cpp and vLLM image adapters are both built, and the request shape stays `design_required` until a live smoke.
      - Pixel size is stored before EXIF orientation.
      - A referenced artifact is reused.
    - The dev pairing, EmbeddingGemma 2 on llama.cpp, has no primary confirmation. Qwen3-VL-Embedding-2B on vLLM has an official example. This goes to you with E5.

## Row moved from STATUS 2026-11-16 09:28 (T-006 accepted row, budget)

  - **T-006 race follow-up and T-006-int: accepted 2026-11-16 00:1x.** Every file mutation in `src/` goes through the anchored layer `storage/_anchored.py` (decision 123 store lock on release; profile bundles anchored at the project root; `publish_new_bytes` no-replace). Reviews: race retest 3 Sonnet PASS and Gemini PASS; T-006-int Sonnet PASS (31 own junction probes refused) and Gemini PASS. Task files: [T-006-race-followup](archive/T-006-race-followup.md), [T-006-int](archive/T-006-int.md); history in the status archive. P3 follow-ups: the AST guard misses `builtins.open`, `io.FileIO`, numeric `os.open` flags, `.save(path)`, nested pyarrow writers and non-literal `.open(mode)` (no such code today); `_ensure_root` in `_write_bytes` untested; the segment refusal reaches `resolve_profile` as a raw `StorageError`, to map when the profile commands are wired; POSIX `linkat` branches run only on Linux CI. **PM decisions pending user review:** round 3 covers the whole class; a failed `release()` becomes a note or `StorageWarning`; profile folder names failing `check_segment` are refused.

## Lines moved from STATUS 2026-11-16 12:46 (CI window 3 red detail, budget)

      - Ubuntu: pipeline suite `2 failed, 2885 passed`; tool suite `1 failed, 5658 passed`. Windows: cancelled.
      - All three failures are POSIX-only tests the Windows dev machine never runs:
        - `test_register_publishes_without_hard_links`: a monkeypatched `os.link` makes the store refuse the platform;
        - `test_posix_lock_file_refuses_a_symlink_leaf`: refused, but with the containment message;
        - `test_remove_link_reports_success_only_when_the_entry_is_really_gone`: a directory link's target is gone afterwards, a possible purge safety defect.

## Lines moved from STATUS 2026-11-16 11:4x (T-021 P2 old-scanner history, S42 state-dir queue item and the E12 row, budget)

  - **E12 (dev GPU smoke): done at 21:08, GPU live, smoke only.** Model `unsloth/Qwen3.5-4B-GGUF` rev `e87f1764` (hashes match `models/manifest.json`) on llama.cpp `b11921` CUDA 13.4. All five combinations of docs/dev-machine.md met target on synthetic images: one image, JSON schema, image plus schema, thinking off, two images in order. Peak VRAM 3676 MiB; teardown back to 0 MiB. Limits: no thinking-on control (the template defaults to non-thinking); not more than two images. Evidence: `runs/E12-20261115-210633/summary.md`.
    - Findings: the server reports `reasoning_format: none`, so any thought text would land in `message.content` (the E10 observer must look there). The server warns that Qwen-VL needs `--image-min-tokens 1024` for grounding. **PM decision pending user review:** the next dev run sets it, since it changes only the dev server.

    - **P2 (report lint) BLOCKED on the user, 2026-11-16 04:05:** fix round 4, the last extra round of decision 121, still FAILS the Codex retest 4 (`.tmp/worktrees/T-021-p2/.tmp/T-021/retest4-p2/review.md`), while Gemini passes it.
      - The two P2 findings are R4F1, where code or HTML inside an image description in a heading skips report discovery, and R4F2, where paragraph rebuilding and heading scans are quadratic and a watchdog fires near 400 KB.
      - Each round closed its own findings and the next retest found a new construct; rounds 3 and 4 also found new defects (R3F1 backtracking, R4F2 quadratic).
      - The old patch stays unintegrated in its worktree.
    - `test_cli_worker_codex.py` and `test_codex_hooks.py` read the live `.tmp/pm-state` ledger, not a test state dir: in main, 42 tests fail with `git_history_changed`. They pass on a clean HEAD, and they pass in main with `AGENT_WORKFLOW_STATE_DIR` set to an empty folder (2026-11-15 18:4x). The override itself breaks 26 other tests (`test_brief_lint` send, `test_pm_supervisor`, a `test_session_context` test), which expect the default state dir: no single setting runs the whole suite green. Fix: each test gets its own state dir.

## Moved 2026-11-16 13:3x (verbatim): S42 CI reds suite counts and CI window 3

    - Main tool suite after the patch: `5394 passed, 11 skipped, 531 deselected in 909.97s`.
    - Codex hook files, run with a scratch `AGENT_WORKFLOW_STATE_DIR`: `530 passed, 1 skipped`.
    - **CI window 3 on `b2273d4`: RED; the repository is private again** ([run](https://github.com/duongve13112002/prompt-enhancer-pipeline/actions/runs/43764148659); report `runs/ci-window/20261115T220152449445Z-b2273d490ef1-33200.json`).
      - Three POSIX-only reds (Ubuntu); the detail moved verbatim to [status-history](archive/status-history-2026-11-14.md).
      - **S42-ci3, 2026-11-16 05:58:** round 1 fixed the three reds. L1 was a code defect: the capability check matched function identity, so a test double over `os.link` looked unsupported. L2 and L3 were test defects. Round 1 also fixed a P0-class `purge_tmp._on_error` that chmodded through links on POSIX. Gemini PASS; Sonnet PASS, plus P2-1 (security): the hook retried the removal by path string, so an ancestor junction redirected it outside the tree, and a probe deleted an outside file. Fix rounds closed it (accepted decisions in decision 128). **Integrated 2026-11-16 12:3x** (Gemini PASS, Sonnet PASS; the P3s, an `nlink>1` retry and RO3, go to the queue). CI window 4 follows.

## Moved 2026-11-16 15:3x (verbatim): T-021 P1 and P2 accepted rows

  - **T-021 (tooling): phase P1 (brief order) accepted 22:2x after fix round 2.** One shared block scanner reads every brief; quote, list and HTML containers are tracked. Codex retest 2 PASS (F4, F5 closed; 4368 legacy comparisons match the base; 102 CRLF/BOM variants match) and Gemini PASS. Applied to main: focused 674 passed. Known conservative limit: lazy continuation followed by `---` is refused as a Setext heading. **PM decision pending user review:** an HTML block is refused only when it holds a heading, underline or fence, so the wrapper's `<goal>` tag lines still pass; no new Markdown library; the new-order lint refuses HTML blocks and headings or fences inside containers instead of parsing them. P2 (report lint) and P3 (scope guard) each follow after their own review. **PM answers, pending user review:**
    - **P2 (report lint):** the old scanner failed retest 4 (history in [status-history](archive/status-history-2026-11-14.md)). **Decision 127:** a rebuild on `markdown-it-py`, by a fresh Opus builder.
      - Rebuild reviews: Gemini PASS. Codex FAIL 10:46, three P2 findings: P2R-F1, a container the parser drops passes silently; P2R-F2, the `reference` title rule is quadratic before the inline cap (2 MB over 55 s), with no turn-end deadline; P2R-F3, diagnostic bytes are unbounded.
      - **Accepted and integrated 2026-11-16 12:3x after fix round 1:** Gemini PASS and Codex retest 1 PASS, with no findings. P2R-F1 to F3 are closed: 4 MB takes 20.7 s, the deadline overshoots by at most 0.38 s, records are about 455 bytes, and 10 of 10 mutants are killed. Its PM decisions (pin 4.2.0, bounds, deadline, exit 2) were accepted in decision 130.
      - **PM integration fix:** `_PARSER_PROBLEM` is now bound once per branch, after the S42-ci3 redefinition scan flagged it. Behaviour is unchanged; another family reviews it with the next tooling review.
      - Residuals: a private `_State` import, guarded by the pin and a test; the standalone CLI has no deadline (about 43 s at 2 MB).

## Moved 2026-11-16 15:3x (verbatim): T-010 P1 row

  - **Pipeline queue:** T-010 (K2 naming and observation, K3 cards, `kb audit`) in three phases; Codex builds P1 after two pre-mortems (Gemini, Claude Sonnet); the task file's PM decisions before P1 are pending user review. P1 Step 0 approved 2026-11-16 01:01. **P1 passed review at 05:5x, after three Codex fix rounds:** Gemini PASS with no findings; Sonnet retest 3 PASS with 4 P3s: `require` is unvalidated and matched by substring (`model_client.py:142`); dead cached-branch code; a late refusal of a wrong suffix; a supplied-plan proof without `require`. PM full suite on the worktree: `3397 passed, 42 skipped in 841.82s`. **P1 accepted and integrated 2026-11-16 06:1x.** **PM decisions pending user review:** the caller states the capability demand, and no generic module reads `knowledge.*`; the 4 P3s become the first items of the P2 builder (fresh Codex, fresh worktree from the P1 commit). **PM decisions pending user review** (answers to its Step 0 Q1-Q5, in `.tmp/T-010/p1-approve.md` and the task file): model-facing `info_assessment.v0` and `folder_naming.v0` without pipeline ids, with a positional `assigned_descriptions`; schema `kb_overrides.v0` for the four documented keys; assessment off plus a mismatched description line is refused; failed K1 decode statuses skipped, null passes; single-behaviour settings are audited as constrained exceptions. E12 live smoke after P3.

## --help



## S42 CI reds (moved 2026-11-16 17:5x)

  - **S42 CI reds: accepted and integrated 2026-11-16 04:30.** Fix rounds 1 and 2 close the CI-red tool tests on both readings, under POSIX and Windows. Reviews: Sonnet review 3 PASS, Gemini fix 2 PASS.
    - Suite counts and the CI window 3 detail (S42-ci3 closed it): moved verbatim to [status-history](archive/status-history-2026-11-14.md).
    - **Residuals:**
      - A command inside both bounds can outlast the hook timeout (24.9 s worker, 30.9 s PM mode): no whole-evaluate wall-clock budget.
      - Hooks fail open on timeout; decision 126: each guard gets its own budget and denies when it is spent (S42 follow-up).
      - A budget-per-target mutant survives.
      - The bound 512 was calibrated on one machine.
    - **PM decision pending user review:** both readings on POSIX; the bounds 64 and 512.

## S42-statedir (moved 2026-11-16 20:0x)

  - **S42-statedir: Codex retest 3 PASS 10:55; integrated 2026-11-16 12:3x with S42-ci3 and T-021 P2.** Main tool suite: `6342 passed, 15 skipped`, plus 1 failure, fixed by the PM integration fix under T-021 (337 passed on rerun); main pipeline suite `3856 passed, 43 skipped`. Every tool test gets its own state directory, so the tool suite runs in one pass. Fix round 3 normalizes Win32 spellings (`abspath` before `realpath`, with property tests); tool suite 5968 passed. Its earlier PM decisions were accepted in decision 128.
    - **PM decisions pending user review:**
      - fix round 3, one round past the limit;
      - the guard covers accidental access in a stable layout, and races are out of scope;
      - R3F1, the PM's own doc fix: `normpath` drops a trailing separator, so a distinct `\\?\` name ending in a space is refused (a conservative false refusal).
    - **Deviation:** the builder fetched one public Microsoft documentation page for quotes despite "no network". It disclosed this and sent no data.
