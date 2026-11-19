# T-016 P5: deployment run guide

Status: **Step 0 approved by the PM (2026-11-19); built; Completion report below; ready for review.** Mode: "Implement the change" (documentation; code only if a command disagrees with its docs, then reported, not fixed, unless the doc can be fixed instead). Requirements: R-001, R-004, R-009, R-015, R-018, R-020, R-024, R-026, R-027. Pipeline intent check (decision 170, AGENTS.md section 10) runs on every sentence of the guide that touches the admin prompt, retrieval off, or knowledge-base and image text.

## Step 0

### Read so far (source-verified, nothing run)

`__main__.py` (parser and dispatch), `cli_commands.py` (handlers, exit mapping), `config.py:45-103` (`COMMAND_READS`), `docs/execution.md` (all), `docs/serving.md` (all), `docs/stages/G-evaluation.md`, `README.md`, `evalset/README.md`, `configs/enhancer.example.yaml` serving and evaluation blocks, `configs/overlays/deploy-vllm.example.yaml` sentinel lines, the T-016 task file (phase table, P1 to P3 reports, PM answers) and the P4 PM integration, decisions 154 and 170 to 172. Not yet read (to read at build, before the sentence that depends on it): `cli_enhance.py`, `cli_serve.py`, `eval_run.py`, `eval_judge.py`, `k0_sync.py` and `kb_sync.py` output shape, `profiles.py` outputs, `configs/enhancer.dev.example.yaml`, `docs/architecture.md`, `docs/dev-machine.md`, `docs/stages/K0-sync.md`, `docs/stages/P-profiles.md`.

### Unknowns and what I already see

1. **The real `--help` text of every subcommand** is not captured yet. Plan: run `python -m prompt_enhancer <group> <command> --help` (no config, no side effect, PYTHONPATH = this worktree's `src`) for the 17 parser leaves and quote the output in this file; the guide's flags are copied from it.
2. **Exit codes and outputs per command** are known only from the handler code: `kb sync`/`kb audit` exit 1 on configuration or store errors (`__main__.py:106-108`), every other command 0/1/2 (`cli_commands.py:186-209`); `kb status` with no store reports `store_present: false`, exit 0 (`cli_commands.py:47-61`); `eval build` and `eval fit` and `kb sync --only` refuse with exit 2 (`NOT_SERVED`, `kb_only`). What `kb sync` prints and which paths `eval run`, `eval import`, `profile register` write is still to read (`k0_sync.py`, `eval_run.py`, `eval_judge.py`, `profiles.py`).
3. **Stale statements found while reading (class sweep):** `README.md:3` says "No pipeline code yet"; `docs/serving.md:3` says "Design only; nothing was implemented" and `:44-50` describes a run guide not yet written; `docs/stages/G-evaluation.md:3` is current for the runner. `docs/execution.md` "Commands" already lists every command including `eval run`/`eval import` (P4 integration), so the reconciliation is a row-by-row check against the parser and handlers, not a rewrite.
4. **Version-specific model-server commands** (vLLM or SGLang flags for thinking default, image limit, memory fraction, prefix cache) are unverified (`docs/serving.md:44-50`, `:50` "Not verified"). No network and no installed server here, so the guide cannot state them as commands.
5. **Where the one full recompute after `k0-scan-v6` / `k1-image-4` shows up for the operator** (what `kb sync --plan` reports, how long it is not stated; no duration may be invented) is read at build from `k0_sync.py`/`stage_guard`, `docs/stages/K0-sync.md`.
6. The first-deployment order in the brief omits the admin profile steps. `enhance once` needs an active profile (`docs/execution.md` row `enhance once`; D3: none active gives an honest `failed` envelope, exit 0), so `profile register` and `profile check` must come between `kb status` and `enhance once`.

### Ranked questions, each with a proposed answer

1. **Include `profile register` and `profile check` in the guide and in the first-deployment order?** Proposed: yes, after `kb status` and before `enhance once`; they are built commands (`cli_commands.py:117-147`), `profile check` activates a profile, and the guide says the operator (the admin) confirms the manifest and its conflicts by hand and never edits the system prompt text (R-004).
2. **How does the guide treat the model-server start command?** Proposed: it gives no vLLM or SGLang command line. It lists what the profile must bind (repository and revision `pin_required`, endpoints `deploy_required`), tells the user to take the flags from the installed version's own `--help` and N121, and marks "server command not verified here" as `not_run` with owner T-017/user. The embedding server command is shown only if its `--help` is captured (`python -m prompt_enhancer.embed_server`, separate CLI).
3. **Figures.** Proposed: no value for any `deploy_required`, `fit_from_bench`, `design_required`, `pin_required` or `ask_user` key; the guide shows `<value>` placeholders and names the sentinel and the task that fits it (T-017 for `fit_from_bench`/`fit_from_pilot` pilot values, T-030 for fitted values, the user for `deploy_required` and `ask_user`). The dev example's four `serving.http` values (4, 10, 10, 10, 23068672) are dev-only and are quoted only as such (D13), never as a recommendation. No latency, throughput or VRAM figure.
4. **A test that the documented commands exist.** Proposed: add `tests/test_run_guide.py` (no runtime change): extract every `python -m prompt_enhancer ...` line from fenced blocks of `docs/run-guide.md` and `docs/execution.md`, and assert each parses with `build_parser()` (placeholders substituted), plus that every parser leaf is named in the guide. It catches a flag the code lacks or a command the guide omits. Approve, or the check stays a manual `--help` comparison quoted here.
5. **Scope of the stale-text fixes.** Proposed: edit only what the commands make false or incomplete: `README.md` (add a link to the guide, correct the "No pipeline code yet" status clause to what is built, without a new claim of quality), `docs/serving.md` (replace the "Run guide" paragraph's future tense with a link, keep "Not verified" and the design-only wording about performance), `docs/execution.md` (link, row fixes found by the audit), `docs/stages/G-evaluation.md` only if a command fact there is wrong. Anything broader goes under `Noticed, not done`.

### Outline of `docs/run-guide.md` (target about 20 KB; one fact once, linking `docs/execution.md` for design and flow)

0. Scope and rules: what this guide is for (R-020, the user runs the deployment, the agent never does), the reading order, what it does not cover (model choice, thresholds); notation (`<project>`, PowerShell and POSIX forms).
1. Before anything: environment and invocation (project `.venv` only; set `PYTHONPATH`/install form as the checkout supports, both shells), the shared flags `--config`, `--overlay`, `--project-root`, the no-link path rule, exit-code table (0/1/2, and the `kb sync`/`kb audit` exception), stdout JSON versus stderr diagnostics.
2. The deployment overlay: how to start from `configs/overlays/deploy-vllm.example.yaml`; a table of every sentinel key the guide's commands read (key, sentinel, who fills it, the task that fits it); the three pipeline-intent rules (admin system prompt used verbatim and never edited, R-004; retrieval off reads no knowledge base, R-009; knowledge-base and image text are data, R-026).
3. GPU and VRAM checks before a model run (read-only: `nvidia-smi` form for PowerShell and POSIX, record holder and baseline per device, wait if held, VRAM back at baseline after, stop every server you started); `runtime.gpus` and `runtime.vram_budget_gb` are profile values (`deploy_required`), no figure given.
4. One section per command, each with the same six items: needs (profile keys from `COMMAND_READS`, with sentinels), exact invocation (both shells where they differ), success output and written paths under `data_root`, exit codes, what to check, and what refuses: `kb sync` (plan first, guard, `--force`, one full recompute after the `k0-scan-v6`/`k1-image-4` bump, K4 per-concept alias failure still publishes), `kb status`, `kb gc`, `kb rollback`, `kb show`, `kb audit`, `profile register`, `profile check`, `enhance once` (request file, attachments written first), `enhance serve` (routes, bounds, stop), `eval run`, `eval import`; then the refusing ones: `eval build`, `eval fit`, `kb sync --only` (exit 2, with owner).
5. First deployment, in order: overlay; VRAM baseline; `kb sync --plan`; `kb sync`; `kb status`; `profile register`, admin confirmation, `profile check`; `enhance once` smoke for generate and edit, retrieval on and off; `enhance serve` with `GET /health` and one `POST /v1/enhance`, then a clean stop; `eval run --variants both` (decision 172) on the named set and snapshot; the judge pack scored by Claude and Gemini outside the product runtime (decision 154); `eval import` (labels `agent_judged`, pilot evidence only, never thresholds); resume and rollback.
6. What the user pastes back (a paste-back checklist: commands, exit codes, printed JSON, VRAM before and after, versions) and what stays `not_run` here (live model, dev GPU, deployment, performance: T-017 and the user).

### Plan (one item in progress at a time; two task-file lines per step)

1. Capture `--help` of the 17 parser leaves and read the unread sources listed above; record what each command prints, writes and exits with, citing file and line. **Finish:** a table in this file with one row per command, every cell with a citation or `not_read`.
2. Write `docs/run-guide.md` from that table. **Finish:** every flag in it appears in the quoted `--help`; every key named appears in a `COMMAND_READS` set or in a cited reader; no number without a source.
3. Reconcile `docs/execution.md` "Commands", `docs/serving.md`, `README.md`, the example-profile comments and `evalset/README.md` (class sweep). **Finish:** a sweep table (file, checked, changed or unaffected).
4. Run the test sets of the brief one file per process through `test_slot.py` (`--basetemp .tmp/T-016e/pytest`, removed at the end) plus `tests/test_run_guide.py` if Q4 is approved. **Finish:** real output pasted. Mutants only if code changes (not expected).
5. Fill Pre-mortem, Completion report, Configuration audit, `not_run`, `Noticed, not done`; remove `.tmp/T-016e/`. **Finish:** the six pre-mortem checks each carry a quoted result.

### Configuration impact

No runtime edit and no new key. The only possible code-side file is `tests/test_run_guide.py` (Q4), which reads documents and the parser and sets nothing. The Configuration audit at the end lists each key the guide names with its owner file and whether it is a sentinel.

### Progress (PM approved Step 0, 2026-11-19; Q1 to Q5 as proposed)

- Done: `--help` of all 19 parser nodes captured to `.tmp/T-016e/help.txt` (to be quoted below); `cli_enhance`, `cli_serve`, `cli_input`, `eval_run`, `eval_judge`, `kb_sync`, `k0_sync`, `profiles` read; `docs/run-guide.md` written (first version).
- To do: `tests/test_run_guide.py`; fix `docs/execution.md`, `docs/serving.md`, `README.md`; run the test sets one file per process; fill this file (help quote, evidence table, pre-mortem checks, audit); delete `.tmp/T-016e/`.
- Facts learned that the guide relies on: an overlay may set only `storage`, `runtime`, `serving`, `vlm.backends` (`config.py:616-622`), so every other sentinel needs a profile that `extends` the base; a UTF-8 BOM or UTF-16 request file is refused (experiment, `.tmp/T-016e/bom.py`).

## Pre-mortem (decision 169)

| # | How the guide could mislead | Check and result |
| --- | --- | --- |
| 1 | a command or flag that does not exist | every `python -m prompt_enhancer` line of the guide and of `docs/execution.md` is parsed by `build_parser()` in `tests/test_run_guide.py`, and the commands table and the guide must name exactly the 14 parser commands (4 passed). Mutants M1 (a choice value), M2 (refusing command not shown), M5 (unknown flag) are killed. The `--help` text is quoted below. The `embed_server serve` and `nvidia-smi` lines are not parsed by that test: `embed_server serve --help` was read (flags `--config`, `--overlay`, `--project-root`), `nvidia-smi --help-query-gpu` and `--help-query-compute-apps` list `index`, `name`, `memory.used`, `memory.total`, `pid`, `used_memory` (help text only; no GPU query was run) |
| 2 | a key a command does not read, or a needed key missing | section 3 table built from `COMMAND_READS` (`config.py:48-103`) by a read-only script over the example base plus the vLLM overlay (`.tmp/T-016e/sentinels.py`, deleted at the end); the Configuration audit lists every key with its owner. Residual: it is a static scan, conditional reads (verifier, fallback backends) are not expanded; the guide says the loader names the first sentinel it meets |
| 3 | an exit code or output path that differs from the code | each statement is cited in the evidence table below; paths: `eval/eval_results/` from `storage/store.py:1376,1391`, `eval/runs/<run_id>/` from `eval_run.py:39,117` and `store.py:1437-1442`, pack directory `packs/<pack_id[7:23]>` from `eval_judge.py:105`, attachments from `attachments.py:22-23` |
| 4 | an OS-specific path or shell syntax as the only form | commands are one line each with forward-slash relative paths, valid in PowerShell and POSIX; the environment, `curl.exe` versus `curl`, the UTF-8-without-BOM request file and the stop signals have both forms. Residual: not run on Linux here (both-OS CI is the PM's) |
| 5 | a measured-looking number with no source | all digits of the guide were listed (grep): they are R-IDs, decision numbers, section numbers, exit codes, HTTP codes (documented in `docs/execution.md`), the example's `min_scorers: 2` (config literal), "16 hex digits" (`eval_judge.py:105`), PowerShell 5.1 and decision 172's "about twice the model calls". No latency, VRAM, threshold, capacity or duration figure; the dev-only `serving.http` values are not quoted |
| 6 | an instruction that edits the admin prompt or bypasses retrieval-off | the guide's section 1 says the prompt is never edited by it and retrieval off opens nothing; the only edit it asks of the admin is `manifest.yaml` (the `_MANIFEST_HEAD` text at `profiles.py:194-195` says the same). The retrieval-off smoke uses a profile copy with `rag.enabled: false`, never a request field; `eval run` refuses `--snapshot` when no scenario is on (`eval_run.py:153-154`) |
| 7 | a refusing command shown as working | section 5.9 lists `eval build`, `eval fit` and `kb sync --only` as exit 2 with their owner; `NOT_SERVED` and `kb_only` (`cli_commands.py:27-44`) and the `--only` dispatch (`__main__.py:84-86`) agree |

## Completion report

**Behaviour.** New `docs/run-guide.md` (about 33 KB): rules (R-004, R-009, R-026, R-001, R-024), interpreter and both shells, flags, exit codes, the profile files (an overlay may set only `storage`, `runtime`, `serving`, `vlm.backends`, so every other sentinel is set in a profile that `extends` the base) with a sentinel table per command and who fills each kind, GPU/VRAM checks, one section per command (needs, invocation, prints and writes, exit, check), the commands that refuse, the first-deployment order (with `profile register` and `profile check`, PM Q1), paste-back and `not_run`. No vLLM/SGLang command (Q2), only `<value>` for sentinels (Q3). Reconciled: `docs/execution.md` (link; rows of `kb rollback`, `kb gc`, `kb audit`, `kb show`, `profile register`, `profile check` fixed to the code), `docs/serving.md` (status wording, run-guide paragraph), `README.md` (status clause, link). New `tests/test_run_guide.py` (Q4). No runtime change, no new key.

**`--help` quoted** (real output of `python -m prompt_enhancer <node> --help`, PYTHONPATH this worktree's `src`; the full capture was `.tmp/T-016e/help.txt`, removed at the end). Top: `usage: prompt_enhancer [-h] {kb,profile,enhance,eval} ...`; `kb`: `{sync,audit,status,rollback,gc,show}`; `profile`: `{register,check}`; `enhance`: `{once,serve}`; `eval`: `{build,fit,run,import}`. Every leaf takes `--config CONFIG` (required, "profile path, relative paths start at the project root"), `[--overlay OVERLAY]`, `[--project-root PROJECT_ROOT]` ("project root (default: the root of this checkout)"). Leaf-specific usage lines:

```text
kb sync     [-h] --config CONFIG [--overlay OVERLAY] [--project-root PROJECT_ROOT] [--plan] [--force] [--only ONLY]   (--only: "refused: a scoped sync is not available")
kb status | kb gc | enhance serve | eval build | eval fit     (common flags only)
kb rollback ... snapshot          kb show ... concept ("a concept id or a concept name")
kb audit    ... --folder-count FOLDER_COUNT --card-count CARD_COUNT --seed SEED
profile register | profile check ... --profile-id PROFILE_ID
enhance once ... --request REQUEST   ("request.v0 JSON file inside the project root")
eval run    ... --set SET [--snapshot SNAPSHOT] --variants {record,both} --split {fit,test,all} [--source-images SOURCE_IMAGES]
eval import ... --run-id RUN_ID --scores SCORES   ("a score file inside the project root; repeat for each scorer")
python -m prompt_enhancer.embed_server serve [-h] --config CONFIG [--overlay OVERLAY] [--project-root PROJECT_ROOT]
```

**Evidence for the guide's exit codes, outputs and paths** (source-verified, read in this task; nothing run against a model):

| Statement | Source |
| --- | --- |
| dispatch: `enhance once/serve` and `kb sync --only` own paths; `kb sync`/`audit` legacy (exit 1 on configuration or store error); all else `run_command` | `__main__.py:74-109` |
| `run_command` exits: `Refusal`/`ConfigurationError` 2; `ProfileError` 1 only for `PROFILE_DRAFT_FAILED`/`PROFILE_GOLDEN_FAILED`, else 2; `StorageError`, `ModelCallError`, other 1; `eval build`/`eval fit`/`kb sync --only` refuse | `cli_commands.py:24-30,186-209` |
| `kb status` keys, exit 0 without a store; `kb show` refusals; `kb rollback` needs a `retired` snapshot and an existing store; `kb gc` prints the report fields | `cli_commands.py:47-114`; `storage/store.py:742-751` |
| `kb audit` prints `status` `OK`/`SNAPSHOT_MISSING` | `kb_audit.py:58-84` |
| `kb sync` statuses `skipped`, `planned`, `unchanged`, `published`; `alias_failures`; one recompute after the `k0-scan-v6`/`k1-image-4` bump (revisions are part of the sync identity, a different identity is not "unchanged") | `kb_sync.py:196-233,283,303-306,169-172`; `k0_checkpoint.py:18`; `k1_embed.py:55`; `execution.md` "Artifact keys" |
| `kb sync --plan` exit 1 when the guard is exceeded without `--force` | `__main__.py:103-105` |
| `profile register`: `PROFILE_EXISTS`, status `draft`, one drafter call, output keys; confirmation is a manual `manifest.yaml` edit | `profiles.py:274-335`, `cli_commands.py:117-131`, `profiles.py:194-195,345-346` |
| `profile check`: removes the old `activation.json`, writes `golden_results.json` always, `activation.json` only on success; refusal codes; the request read set is checked first | `profiles.py:609-695`; `cli_commands.py:134-147` |
| `enhance once`: order of effects, attachments `<data_root>/attachments/<trace_id>/<hex>` + `index.json`, exit 0/1/2, `operational:` exit 1 | `cli_enhance.py:25-85`; `attachments.py:22-23`; `cli_input.py:128-149` |
| a BOM or UTF-16 request file is refused | experiment `.tmp/T-016e/bom.py`: `request: not valid JSON (line 1, column 1)`, `request: not UTF-8 at byte 0`; plain UTF-8 parses |
| `enhance serve`: stderr lines, exit 0/1/2, stop signals, routes and HTTP codes | `cli_serve.py:428-464,484-542`; `execution.md` "`enhance serve`" |
| `eval run` refusals (before any call, exit 2), snapshot rules, `--source-images` rule, run id, manifest, summary keys, `run_eval` exit and failure text | `eval_run.py:141-204,219-300,452-473`; `cli_commands.py:150-168`; `eval_set.py:201-207` (file named by the hex digest) |
| data layout `eval/eval_results/`, `eval/runs/<run_id>/{manifest.json,summary.json,incomplete/,packs/<16 hex>/{key.json,pack.jsonl},imports/<16 hex>.json,run.lock}` | `storage/store.py:1376,1391,1437-1442,1484-1495`; `eval_run.py:39,117`; `eval_judge.py:105,121-123,262` |
| `eval import`: scorer rules, schema, outputs, never writes `eval_results` | `eval_judge.py:183-264`; `contracts/schemas/eval_judge_scores.v0.schema.json` |
| overlay keys `storage`, `runtime`, `serving`, `vlm.backends` only; `overlay_of` must be in the chain | `config.py:615-625,1572-1595,635-650` |
| sentinel groups per command | `config.py:48-103` over `configs/enhancer.example.yaml` + `configs/overlays/deploy-vllm.example.yaml` |

**Commands run** (each file in its own process through the limiter, `PYTHONNOUSERSITE=1`, `PYTHONPATH` this worktree's `src`, `--basetemp .tmp/T-016e/pytest`, interpreter `<project>/.venv/Scripts/python.exe`):

| File | Result |
| --- | --- |
| `tests/test_cli_commands.py` | 12 passed in 38.87 s |
| `tests/test_cli_config.py` | 70 passed in 7.02 s |
| `tests/test_eval_cli.py` | 9 passed in 109.79 s |
| `tests/test_field_contracts.py` | 532 passed, 34 skipped in 306.77 s |
| `tests/test_run_guide.py` (new) | 4 passed in 0.48 s |

No other test reads the changed documents (grep of `tests/` for `execution.md`, `serving.md`, `README.md`: only a docstring in `test_enhance_serve_config.py` and `test_embed_server_http.py`). `test_cli_commands.py` shows 12 tests here against the 14 recorded in the P3 report; I did not investigate the difference (no change of mine touches it).

**Mutants** (scratch copy of the five files, `.tmp/T-016e/mutate.py`, removed; each killed by one test of `test_run_guide.py`): M1 guide `--variants both` to `all`; M2 `eval fit` line removed from the guide; M3 commands table `kb gc` to `kb collect`; M4 README link removed; M5 guide `--folder-count` to `--folders`; M6 `kb show` row removed from the table; M7 serving link removed: 7 of 7 killed. No source code changed, so no source mutants.

**Class sweep** (docs that list commands or options):

| File | Result |
| --- | --- |
| `docs/execution.md` | changed: link, six rows fixed; the rest checked against the handlers |
| `docs/serving.md`, `README.md` | changed: status wording and the link |
| `docs/stages/G-evaluation.md` | checked, unaffected: the runner paragraph agrees with `eval_run.py`/`eval_judge.py` (pack, lock, exits) |
| `evalset/README.md` | checked, unaffected: table names and the independence rules; generated data lives under `data_root/eval/` as the guide says |
| `docs/stages/P-profiles.md`, `K0-sync.md`, `K4-aliases.md`, `model-storage.md`, `architecture.md`, `contracts/README.md`, `prompts/README.md` | checked, unaffected (command mentions agree) |
| `docs/stages/K3-cards.md:25` | **inconsistent**, not edited (Q5): says the lint report reaches the admin through `kb show`; `kb show` prints no lint (`execution.md` row, `cli_commands.py:64-83`). `K3-cards.md:34` (`kb audit` flags) agrees with the parser |
| `configs/*.example.yaml`, `configs/overlays/deploy-vllm.example.yaml` comments | command comments (`serving`, `evaluation` blocks) agree; the three file headers still say "nothing is implemented" / "no loader exists", false now: not edited (Q5) |

**Changed files.** New: `docs/run-guide.md`, `tests/test_run_guide.py`. Edited: `docs/execution.md`, `docs/serving.md`, `README.md`, this file. No Git write; `.tmp/T-016e/` and the pytest basetemp are removed at handoff.

## Configuration audit

No runtime edit, no new key, so no new override or refusal test is owed; the guide only names existing keys. Owner = the reader or validator; override and refusal tests = those of the owning task, not re-run here beyond the files listed above. "Sentinel" = the value in `configs/enhancer.example.yaml` or the vLLM overlay.

| Key (as the guide names it) | Owner | Sentinel in the example |
| --- | --- | --- |
| `--config`, `--overlay`, `--project-root` | `__main__.py:20-25` (CLI), `config.load_config` | no |
| `rag.enabled` | `config.py:60,75-79` (read sets), `kb_sync.py:196`, `eval_run.py:149` | no (a boolean the user sets) |
| `storage.kb_store`, `storage.data_root`, `storage.table_format` | `config.py:59-67,75-101`, `storage/store.py:275-279` | no |
| `storage.lock.timeout_s`, `.poll_s` | `config.py:59-101`, `store.py:1484` | `deploy_required` (overlay) |
| `knowledge.root` and `knowledge.*` | `config.py:60` (`kb sync`) | `knowledge.root` `deploy_required`; `knowledge.naming.rename_on_change` `design_required` |
| `sync.retained_images.max_image_bytes`, `.max_total_bytes` | `kb_sync.py` (`retain_images`), `contracts/README.md:274` | `deploy_required` |
| `sync.snapshots.keep`, `sync.gc.retention`, `sync.mass_deletion_guard.max_removed_fraction` | `config.py:60,65`, `k0_sync.py:105` | `design_required` |
| `embedding.backend`, `.batch_size` / `.revision` / `.dimension_mode`, `.request.*`, `.query_text_limit`, `.query_text_template` / `.chosen`, `.dimension` | `config.py:60,75,94` | `deploy_required` / `pin_required` / `design_required` / `fit_from_gt` |
| `observe.*`, `cards.*`, `index.*`, `aliases.languages` | `config.py:60` | `design_required`, `fit_from_gt`, `fit_from_bench` (`index.build`) |
| `vision.max_decode_pixels`, `vision.quality` | `config.py:63,75-77,94` | `deploy_required`, `design_required` |
| `vlm.roles.<role>.*`, bound `vlm.backends.<name>.*` | `config.py:62-63,72,78,97`, overlay `config.py:616-625` | `design_required`, `pin_required`, `deploy_required`, `fit_from_bench` |
| `profiles.root`, `.exclude`, `.default_profile_id`; `creativity` | `config.py:72-76`, `profiles.py:74` | `default_profile_id` `deploy_required` |
| `runtime.vram_budget_gb`, `.gpus`, `.cpu_workers` | `config.py:49,60,75-78` | `deploy_required`; `gpus: auto`; `cpu_workers: auto` |
| `runtime.trace.*`, `runtime.monitoring.*`, `runtime.request_deadline_s`, `.intake_deadline_s` | `config.py:77,86`, `pipeline.py` | `deploy_required` / `fit_from_bench` |
| `candidates.*`, `assembly.*`, `edit.max_kb_reference_images`, `validation.verifier.*` | `config.py:75-78` | `fit_from_gt`, `fit_from_bench`, `fit_from_pilot`; `require_calibration` `deploy_required` (decision 155) |
| `request_overrides.allowed_keys` | `config.py:1603-1617` | no (`[]`) |
| `serving.host`, `.port`, `.http.{max_connections,max_request_bytes,read_timeout_s,write_timeout_s,shutdown_timeout_s}` | `config.py:85-86`, `validate_serving`, `validate_request_limit` | `deploy_required` |
| `serving.max_concurrency`, `.total_deadline_s` / `.queue_limit` | `config.py:85`, `validate_serving` | `fit_from_bench` / `design_required` |
| `serving.prefix_cache` | not read (`config.py:83-84`) | `true` (an external-backend setup choice) |
| `evaluation.scenario_kinds`, `.split_by`, `.run.{max_set_bytes,max_source_image_bytes,max_concurrency,max_sidecar_bytes}`, `.judge.{blinding_seed,score_min,score_max,rubric_prompt_id,min_scorers,under_test_families,max_score_file_bytes}` | `config.py:91-101`, `validate_evaluation_settings` | `run.*`, `blinding_seed`, `under_test_families`, `max_score_file_bytes` `deploy_required`; `score_min`, `score_max` `design_required`; `min_scorers: 2` (a literal) |

Retained literals added by this task: none in code; the test holds `MODULE = ('-m', 'prompt_enhancer')`, a protocol-bound module name, and the shell operator set `OPERATORS` (protocol-bound shell syntax). No hand-written content rule: the test reads Markdown structure (fenced blocks, table rows, link targets) and the argparse tree, never prose.

## not_run

- Lint and `ruff format --check` (ruff not installed); `tools/agent-workflow/tests/` (no tooling change); the full test suite (one file per process only); both-OS CI (PM; the new test is OS-neutral, written without path separators or shell calls, but ran on Windows only).
- Any live model call, dev-GPU run, deployment run or performance measurement: T-017 and the user. In particular every command's real output on a live backend; the server start commands (version-specific, unverified); `curl` against `enhance serve`; the `nvidia-smi` queries (option names read from its help only); `embed_server serve` start.
- The sentinel table is a static reading, not a run of each command against an incomplete profile.
- The guide's claim that `kb gc` is refused while a `kb sync` holds the writer is not made; it says to run it when no sync runs (the lock behaviour was not exercised).

## Noticed, not done

- `docs/stages/K3-cards.md:25` says the lint report goes to the admin through `kb show`; `kb show` prints no lint (`docs/execution.md`, `cli_commands.py:64-83`). Out of the Q5 list.
- The headers of `configs/enhancer.example.yaml`, `configs/enhancer.dev.example.yaml` and `configs/overlays/deploy-vllm.example.yaml` still say "Documentation only; nothing is implemented" (the example one adds "so no loader exists"): false since the loader exists.
- `docs/stages/G-evaluation.md:3` still says "no scenario set is accepted and nothing is measured yet" (true) but its decisions paragraph (line 44) lists no `evaluation.run.*`/`evaluation.judge.*` keys; harmless.
- An overlay cannot carry non-`vlm.backends` keys, so a deployment needs a profile of its own that extends the base; the vLLM overlay's header does not say so (the guide does). Consider a tracked `configs/enhancer.deploy.example.yaml` (a new file, so a PM decision).
- `kb audit --folder-count` accepts negative integers at the parser (`type=int`); `K3-cards.md:34` says counts are nonnegative. Not checked in `kb_audit.py`.
- `tests/test_probe_monitoring_tmp.py` leftover noted by P3 still needs the PM's deletion.
