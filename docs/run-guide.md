# Run guide: every command, in the order of a first deployment

For the person who runs the deployment (R-020). The agent never logs in to the deployment and never starts its servers (R-018); you run the commands below and paste back what they print. Requirements: R-001, R-004, R-009, R-015, R-018, R-020, R-024, R-026, R-027. Design and flow behind the commands: [execution](execution.md), [serving](serving.md), [G-evaluation](stages/G-evaluation.md), [P-profiles](stages/P-profiles.md).

**State of the evidence.** Every command here is built and tested on the CPU with labelled HTTP doubles. Only `kb sync` has run against live local models, on the dev GPU over a one-folder smoke knowledge base (2026-11-18; report under the Git-ignored `runs/E-kbsync-20261118-225806/`, status `published`); `profile`, `enhance` and `eval` have not, no command has run on the deployment, and nothing is measured (R-021): live runs and performance belong to T-017 and to you. This guide gives no latency, throughput, VRAM, threshold or capacity figure; where a value is needed it names the sentinel and the task that fits it, and you write `<value>` only after that task or you have supplied it.

## 1. Rules the commands keep

- **The admin system prompt is used verbatim and defines the output format (R-004).** This guide never asks you to edit `system_prompt.txt`. A profile records the prompt's hash; if the file changes, `profile check` and every request refuse the profile until it is registered and checked again. Whether and how the admin changes a prompt is the admin's act, outside this guide.
- **Retrieval off reads no knowledge base (R-009).** With `rag.enabled: false` no store, gateway, snapshot or index is opened and no knowledge content is read (`kb sync` still validates its configuration, which resolves the configured `knowledge.root` path without reading it). `kb sync` then returns `{"status":"skipped","reason":"retrieval_disabled"}`, and `eval run` refuses `--snapshot` when every scenario runs with retrieval off. Words such as "rag=false" inside a prompt have no effect: the prompt is data.
- **Knowledge-base text, text read from images and agent score files are data, never instructions (R-026).** No command here follows them; score files have no free-text field.
- **The user's intent survives unchanged (R-001).** `enhance once` and `enhance serve` hand your prompt to the pipeline as written; the request file holds it verbatim.
- **Honest status (R-024).** An envelope with status `failed`, `degraded` or `needs_clarification` is delivered as it is and exits 0; read its `status` field, not the exit code, for the outcome of one request.

## 2. Before the first command

**Interpreter.** `python` below is the project's `.venv` interpreter: `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` elsewhere (activate the environment or spell the path). The package is not installed; it runs from `src`. The embedding server runs in the separate GPU environment `.venv-gpu` ([dependencies-and-environments](dependencies-and-environments.md)).

```powershell
cd <project>
$env:PYTHONNOUSERSITE = "1"; $env:PYTHONPATH = "<project>\src"
```

```sh
cd <project>
export PYTHONNOUSERSITE=1 PYTHONPATH=<project>/src
```

Every command below is one line and runs unchanged in PowerShell and in a POSIX shell. Write paths with forward slashes, relative to the project root. The names `configs/enhancer.deploy.yaml` and `configs/overlays/deploy.yaml` stand for the two files you create in section 3.

**Flags every command takes.** `--config <file>` (required), `--overlay <file>` (optional), `--project-root <dir>` (default: the root of the checkout the code runs from). Relative paths start at the project root and nothing outside it is read. For every command except `kb sync` and `kb audit`, a link or junction on the way to `--config`, `--overlay`, `--request`, `--set`, `--source-images` or `--scores` is refused, also when it stays inside the root. Quote a value that holds spaces with double quotes (both shells accept them).

**Exit status.**

| Code | Meaning |
| --- | --- |
| 0 | done or delivered |
| 1 | operational failure: a model call, a store, a delivery or a bind failed, or a run has failed units |
| 2 | refusal made before any side effect: a bad argument, request, file or configuration value (including a sentinel left in a key the command reads), or a command this build does not serve |

`kb sync` and `kb audit` keep their earlier behaviour: a configuration or store error exits 1, and `kb sync --only` exits 2. Output: one JSON line on stdout, only on success (except `kb sync --plan` over the mass-deletion guard, which prints the plan and exits 1), UTF-8; diagnostics go to stderr and name a field and a keyword, never a prompt, an image or a value of the request. A refusal usually prints `refused: ...` and a failure `failed: ...`; a store error prints its own message.

## 3. The profile files, sentinels and who fills them

`configs/enhancer.example.yaml` is the full profile with every unmeasured value held as a sentinel. A command refuses at start, naming the key, when a key it reads still holds one (R-015). Nothing is guessed and no tracked example is complete for a deployment.

| Sentinel | Meaning | Who writes the value |
| --- | --- | --- |
| `deploy_required` | set per machine or deployment | you, in your overlay or profile |
| `pin_required` | an exact model revision (a commit or file hash) | you, after verifying the artifact ([model-storage](model-storage.md)) |
| `design_required` | decided by the implementing task with evidence | the PM's task for that key; until then the command refuses |
| `fit_from_bench` | measured on the machine that serves | T-017 for the pilot (and E13 benchmarks, [G-evaluation](stages/G-evaluation.md)) |
| `fit_from_pilot` | taken from a pilot on your knowledge base | T-017 |
| `fit_from_gt` | fitted on the evaluation set (`eval fit`, refused in this build) | T-030, from runs on the deployment's models; a dev-machine run never fits one (R-020) |
| `ask_user` | a paid-call budget | you, before the first paid call |

Do not type a value into a `fit_from_*` or `design_required` key to get past a refusal unless the PM's brief for your run names it; a value written that way is a provisional choice of the pilot, never a measured or fitted one, and no figure belongs in this guide.

**Two files, because an overlay may set only `storage`, `runtime`, `serving` and `vlm.backends`.** Every other key (`knowledge`, `embedding`, `profiles`, `evaluation`, `validation`, `candidates`, the role settings under `vlm.roles`, ...) must be set in a profile of your own that extends the base, as `configs/enhancer.dev.example.yaml` does:

1. `configs/enhancer.deploy.yaml`: starts with `schema_version: 2` and `extends: enhancer.example.yaml`, then the keys outside the overlay.
2. `configs/overlays/deploy.yaml`: a copy of [deploy-vllm.example.yaml](../configs/overlays/deploy-vllm.example.yaml) with `overlay_of` naming a file in your profile's chain (the base or your profile) and the per-machine values.

Pass them as `--config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml`.

**Every configured path resolves inside the project root.** Configured paths such as `knowledge.root`, `storage.data_root`, `storage.kb_store` and `profiles.root` are checked after resolving links (`config.py` `_resolve_within`): a path that resolves outside the project root, or into a protected directory such as `models/` or `.secrets/`, is refused, and `knowledge.root` must not overlap `storage.data_root` or `storage.kb_store`. A knowledge base on another drive or mount therefore cannot be named today; place it under the project root (the repository's read-only `database/` folder is where the example knowledge base lives). How the deployment's real knowledge base is placed is an open question for the PM and the user, not something to work around. Credentials never go in these files; they live only in the Git-ignored `.secrets/`, loaded by the project's own code (AGENTS.md section 6).

**Sentinel groups each command reads** (a static reading of the example base plus the vLLM overlay; the loader names the first one it meets, so expect to fix them in a loop):

| Keys | Sentinels in the example | Read by |
| --- | --- | --- |
| `storage.lock.timeout_s`, `storage.lock.poll_s` | `deploy_required` | every `kb`, `profile` and `eval` command |
| `knowledge.root`, `sync.retained_images.max_image_bytes`, `.max_total_bytes` | `deploy_required` | `kb sync` |
| `sync.snapshots.keep`, `sync.gc.retention` | `design_required` | `kb sync`, `kb gc` |
| `sync.mass_deletion_guard.max_removed_fraction`, `knowledge.naming.rename_on_change`, `aliases.languages`, `index.engine`, `index.metric`, `observe.facet_schema_version` | `design_required` | `kb sync` |
| `observe.near_duplicate_cosine_distance`, `observe.representatives_per_folder`, `cards.*`, `index.lexical.ngram_size` | `fit_from_gt` | `kb sync` |
| `index.build` | `fit_from_bench` | `kb sync` |
| `embedding.backend`, `embedding.batch_size` | `deploy_required` | `kb sync`, `enhance once`, `enhance serve`, `eval run` |
| `embedding.revision` | `pin_required` | same |
| `embedding.dimension_mode`, `embedding.request.*`, `embedding.query_text_limit`, `embedding.query_text_template` | `design_required` | same |
| `embedding.chosen`, `embedding.dimension` | `fit_from_gt` | same |
| `vision.max_decode_pixels` | `deploy_required` | `kb sync`, `enhance once`, `enhance serve`, `eval run` |
| `vision.quality` | `design_required` | same |
| `vlm.roles.<role>.*` settings and the bound `vlm.backends.<name>.*` (engine version, endpoints, timeouts, retries, probe, image limits) | `design_required`, `pin_required`, `deploy_required`, `fit_from_bench` | the roles each command uses (`kb sync` K1 to K4 roles; `profile register` the `manifest_drafter`; `enhance`/`eval run` the `planner`, `writer` and `verifier`) |
| `profiles.default_profile_id` | `deploy_required` | `enhance once`, `enhance serve`, `eval run` |
| `runtime.vram_budget_gb`, `runtime.trace.*`, `runtime.monitoring.*` | `deploy_required` | `enhance once`, `enhance serve`, `eval run` |
| `runtime.request_deadline_s`, `runtime.intake_deadline_s` | `fit_from_bench` | same |
| `candidates.*`, `assembly.*`, `edit.max_kb_reference_images` | `fit_from_gt`, `fit_from_bench` | same |
| `validation.verifier.require_calibration` | `deploy_required` (decision 155) | same |
| `validation.verifier.calibration_ref`, `.sampled_fraction_for_rest` | `fit_from_gt`, `fit_from_pilot` | same |
| `serving.host`, `serving.port`, `serving.http.*` | `deploy_required` | `enhance serve` (`serving.http.max_request_bytes` also `enhance once`) |
| `serving.max_concurrency`, `serving.total_deadline_s` | `fit_from_bench` | `enhance serve` |
| `serving.queue_limit` | `design_required` | `enhance serve` |
| `evaluation.run.*`, `evaluation.judge.blinding_seed`, `.under_test_families`, `.max_score_file_bytes` | `deploy_required` | `eval run`, `eval import` (each reads its own leaves) |
| `evaluation.judge.score_min`, `.score_max` | `design_required` | `eval run`, `eval import` |

`serving.prefix_cache` is an external-backend setup choice and is not read by any command. `serving.total_deadline_s` must exceed `runtime.request_deadline_s`, and `serving.http.max_connections` must be at least `serving.max_concurrency + serving.queue_limit` (config-time refusals).

## 4. GPU and VRAM checks before every model run

A command that calls a model (`kb sync`, `profile register`, `profile check`, `enhance once`, `enhance serve`, `eval run`) needs the model servers up; you start them, outside this repository. This guide states no server command line: the thinking default, image limit and memory fraction are version-specific and unverified here ([serving](serving.md), "Not verified"), so take them from the installed server's own `--help` and from the pinned model's card, and use the `pin_required` revision. The embedding server is this project's own: `python -m prompt_enhancer.embed_server serve --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml`, run in `.venv-gpu`; read its `--help` before the first start.

Before each GPU job (AGENTS.md section 8): record the used VRAM of each device and which process holds it; if another session holds it, wait. Afterwards VRAM is back at that baseline and every server you started is stopped. On NVIDIA hardware (the tool's own query options, not run by the agent):

```sh
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

The same two lines work in PowerShell. On Windows the second line often lists no process (WDDM driver mode); then identify the holder another way (for example Task Manager's GPU columns) so the record names it. `runtime.vram_budget_gb` (`deploy_required`, per GPU) and `runtime.gpus` (`auto` in the example) are your profile values. The commands call the servers over HTTP and place no model themselves (R-018); the servers' launch does.

## 5. The commands

Each entry: what it needs, the invocation, what it prints and writes, exit status, what to check. `<data_root>` is `storage.data_root` (default `data/`, Git-ignored); `<kb_store>` is `storage.kb_store`.

### 5.1 `kb status`

- **Needs:** `storage.kb_store`, `storage.lock`.
- **Run:**

```sh
python -m prompt_enhancer kb status --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **Prints:** one JSON line: `store_present`, `snapshots` (every registry row), `current` (`snapshot_id`, `manifest_sha256`, `source_scan_summary`, or `null`), `tables` (row counts of `folders`, `concepts`, `images`, `cards`, `aliases`), `folders_with_error`, and `live_source_changes: "not_checked"`. Writes nothing that remains (when a snapshot exists it takes and releases a lease under the store lock).
- **Exit:** 0, also when no store exists (`store_present: false`, `current: null`); 2 for a configuration refusal.
- **Check:** `current.snapshot_id` is the id you pass to `eval run --snapshot`; `folders_with_error` is 0 or understood; `source_scan_summary.k4_failed` is absent (present means some concepts have no generated aliases). It does not look at the source folders: `kb sync --plan` shows pending changes.

### 5.2 `kb sync`

- **Needs:** the `kb sync` groups of section 3 and the K1 to K4 model servers up. With `rag.enabled: false` it returns `skipped` and touches nothing (R-009). A model-calling sync runs only with your go; a paid backend needs a call budget you gave (`ask_user`).
- **Run, plan first:**

```sh
python -m prompt_enhancer kb sync --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml --plan
```

```sh
python -m prompt_enhancer kb sync --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

Add `--force` only after reading the plan: it overrides the mass-deletion guard, which stops a sync that would remove more than `sync.mass_deletion_guard.max_removed_fraction` of the knowledge base (for example when the knowledge folder is empty or was moved). `--only <folder>` is refused (exit 2): a scoped sync would treat the other folders as removed; run the whole knowledge base.
- **One full recompute.** A store built before the revisions `k0-scan-v6` (K0 scan) and `k1-image-4` (K1 image embeddings) is not "unchanged": the next `kb sync` recomputes those two stages' artifacts once and publishes a new snapshot ([execution](execution.md), "Artifact keys"). A store never synced does a full first run anyway. The guide states no duration or call count; `--plan` prints the work (`model_calls.image_inputs` and the roles), and a rerun after an interruption resumes (R-017).
- **Prints:** `--plan`: `status: planned`, `plan` (the diff), `report` (including `guard`: `removed`, `previous`, `fraction`, `threshold`, `exceeded`, `forced`) and `model_calls`; no snapshot is written. A run: `status: published` with `snapshot_id`, `report`, `stages` and `index`, or `status: unchanged` (with `publish_skipped: identical` when only the same alias failures repeat). Writes under `<kb_store>`: artifacts, the new snapshot and the current pointer, which moves last.
- **Exit:** 0 published, unchanged, skipped, or a plan the guard permits; 1 a plan the guard would refuse without `--force`, a configuration or store error, a failed stage (`<stage>: STAGE_FAILED`) or an unreadable source (`k0: SOURCE_READ_FAILED`). A failed K4 alias key does not block publishing (decision 165): the result gains `alias_failures` (ids and closed codes, never model text) and the exit is 0; a rerun retries only those keys.
- **Check:** `status`; `report` lists no unreadable or protected folders; `alias_failures` empty or noted; then `kb status`.

### 5.3 `kb gc`, `kb rollback`, `kb show`, `kb audit`

```sh
python -m prompt_enhancer kb gc --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
python -m prompt_enhancer kb rollback <snapshot-id> --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
python -m prompt_enhancer kb show <concept> --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
python -m prompt_enhancer kb audit --folder-count <n> --card-count <n> --seed <n> --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **`kb gc`** needs `sync.gc.retention`, `sync.snapshots.keep`, `storage.*`. Removes unreferenced snapshots and artifacts past retention; it never runs inside `kb sync`. Prints `collected`, `kept`, `leased`, `held`, `session`, `skipped`, `damaged`, `artifacts_removed`, `artifacts_marked`. A missing store is refused (exit 2); it does not create one. Run it when no `kb sync` is running (one writer at a time).
- **`kb rollback <snapshot-id>`** moves the current pointer to a retained (`retired`) snapshot; prints `{"command":"kb rollback","current":"<id>"}`. Refused (exit 2) for an unknown, malformed, collected or already-current id. Requests already running keep the snapshot they pinned.
- **`kb show <concept>`** prints the card, aliases and folder bindings of a concept given by id or name from the current snapshot (exit 2 when there is no snapshot or no such concept). Read only.
- **`kb audit`** samples folders and cards for human checking (E16): `--folder-count`, `--card-count` and `--seed` are your choices (non-negative integers, all required). Prints `status` (`OK`, or `SNAPSHOT_MISSING` with exit 0), `snapshot_id`, `folders`, `cards`, `actual`. Read only.

### 5.4 `profile register` and `profile check`

The admin's profile folder `<profiles.root>/<profile-id>/` (default `configs/profiles/<profile-id>/`) holds `system_prompt.txt` before registration; the folder name is the profile id. `configs/profiles/_example/` is an excluded mock, not a deployment profile.

```sh
python -m prompt_enhancer profile register --profile-id <profile-id> --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
python -m prompt_enhancer profile check --profile-id <profile-id> --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **`profile register`** needs `profiles.*`, `creativity`, the `manifest_drafter` role (its server up) and `storage.lock`. One model call drafts `manifest.yaml` with status `draft` and the conflict list; activates nothing. Prints `profile_id`, `status` (`draft`), `overrides`, `findings`, `call_status`. Exit 2 when `manifest.yaml` already exists (`PROFILE_EXISTS`) or the prompt is invalid; 1 when the drafting call fails (`PROFILE_DRAFT_FAILED`). The system prompt file is only read.
- **Between the two commands the admin confirms the manifest** by editing `manifest.yaml` (never `system_prompt.txt`): `status: confirmed` and one decision per conflict (`profile_wins`, `config_wins` or `block_activation`) with `decided_by_admin: true`. The admin also writes the golden cases as `golden/<id>.yaml` in the profile folder ([golden_case.v0](../contracts/schemas/golden_case.v0.schema.json), detail in [P-profiles](stages/P-profiles.md)) and lists their ids under `golden_tests` in `manifest.yaml` (registered empty); `profile check` refuses an empty list (`PROFILE_GOLDEN_MISSING`) and a conflict without a case (`PROFILE_GOLDEN_UNCOVERED`). The pipeline infers no rule.
- **`profile check`** needs the whole request read set of section 3 (it runs the golden cases through the production pipeline), the planner, writer and verifier servers up (and the embedding server when retrieval is on), a confirmed manifest and golden cases (`golden/<id>.yaml`). It first removes any earlier `activation.json`, writes `golden_results.json`, and writes `activation.json` only when every case passes and no conflict blocks. Prints `activated: true`, `activation` and `results`. Exit 2 for an unconfirmed manifest, undecided or blocking conflict, or a changed prompt hash; 1 when a golden case fails (read `golden_results.json`) or a model call fails.
- **Check:** `activation.json` exists; any later change to the prompt, manifest, golden cases, addendum or schemas, to the checking code or its libraries (a new checkout), or to the value of a configuration key a conflict cites (for example a profile copy with `rag.enabled: false` when a conflict cites `rag.*`) makes the profile inactive (`PROFILE_NOT_CHECKED`) until `profile check` runs again for that configuration (`profiles.py` activation fingerprints).
- **A changed system prompt** needs a new draft: `profile register` refuses while `manifest.yaml` exists, so the admin removes the draft manifest (never the prompt), registers, confirms and checks again.

### 5.5 `enhance once`

- **Needs:** the request read set of section 3, `serving.http.max_request_bytes` (bounds the request file), an active admin profile, the planner, writer and verifier servers up, and, with retrieval on, a published snapshot and the embedding server up. With retrieval off none of the knowledge groups is opened (R-009).
- **Request file:** one JSON object inside the project root, strict UTF-8 **without a byte-order mark**, no duplicate keys: `{"prompt": "<your prompt>", "task": "generate"}`; an edit adds `"source_image": "<base64>"` (required for `edit`, refused for `generate`); optional `"profile_id"` (else `profiles.default_profile_id`) and `"overrides"` (only keys listed in `request_overrides.allowed_keys`, which is empty in the example). Windows PowerShell 5.1 `>` and `Out-File` write UTF-16, and `Set-Content -Encoding UTF8` writes a BOM; both are refused (`request: not UTF-8 at byte 0`, `request: not valid JSON (line 1, column 1)`, observed on the parser). Save the file from an editor as UTF-8 without BOM, or in PowerShell create the folder first (`New-Item -ItemType Directory -Force data\requests`, inside the Git-ignored `data_root`) and use `[System.IO.File]::WriteAllText("<project>\data\requests\r1.json", $text, (New-Object System.Text.UTF8Encoding $false))`.
- **Run:**

```sh
python -m prompt_enhancer enhance once --request data/requests/r1.json --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **Prints:** the `response_envelope.v0` as one JSON line (`status`: `success`, `repaired`, `degraded`, `needs_clarification` or `failed`; `output`; `attachments`; `trace_id`). When the envelope lists `attachments`, each image is first written to `<data_root>/attachments/<trace_id>/<sha256 hex>` next to `index.json` (hashes, sizes, expiry `runtime.trace.retention_s`), then the envelope is printed, then the response is acknowledged. The trace store under `<data_root>/traces/` is written by the pipeline. A later run removes expired attachment sets.
- **Exit:** 0 delivered (any envelope status); 1 a model call, publication, stdout write or lifecycle failure (no envelope printed and nothing acknowledged when the delivery failed; `operational: <codes>` on stderr also exits 1); 2 a refused argument, file, request or setting before any model call.
- **Check:** the envelope `status` and `warnings`; with retrieval on and no published snapshot the request still runs and returns `degraded` with `SNAPSHOT_MISSING`; with no active profile it returns a `failed` envelope with exit 0, so run `profile check` first; `attachments` exist as files.

### 5.6 `enhance serve`

- **Needs:** as `enhance once` minus the request file, plus `serving.host`, `serving.port`, `serving.max_concurrency`, `serving.queue_limit`, `serving.total_deadline_s` and `serving.http.{max_connections,max_request_bytes,read_timeout_s,write_timeout_s,shutdown_timeout_s}`. No host or port flag exists. The tracked dev example holds small dev-only values for the `http` leaves, none measured; they are not a recommendation. Section 4 checks first.
- **Run:**

```sh
python -m prompt_enhancer enhance serve --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **Prints:** nothing on stdout; stderr has `listening on <host>:<port>`, then `stopping: N connection(s) open` and `stopped: cancelled requests N` at the stop, plus warnings and `failed: ...`. Routes (standard library server, one request per connection): `GET /health` returns `{"status":"ok"}` without a model call; `POST /v1/enhance` takes one `request.v0` JSON body (`Content-Type: application/json`, a `Content-Length`) and returns the envelope with HTTP 200 for every envelope status; `GET /v1/attachments/<trace_id>/<image_id>` returns a published copy (404 unknown or expired, 500 damaged). A refused request gets `{"error":{"code","message"}}` with 400, 404, 405, 408, 411, 413, 415, 417, 431, 501, 503 (overloaded or stopping), 504 (service deadline) or 500. Writes attachments as for `enhance once`.
- **Try it** (HTTP client not part of this repo; on Windows use `curl.exe`, because `curl` in PowerShell is an alias of `Invoke-WebRequest`):

```sh
curl -s http://<host>:<port>/health
curl -s -X POST -H "Content-Type: application/json" --data-binary @data/requests/r1.json http://<host>:<port>/v1/enhance
```

- **Stop:** Ctrl+C (also Ctrl+Break on Windows) in its console, or `kill -TERM <pid>` (POSIX). `taskkill /F`, `Stop-Process -Force` and `kill -9` are not a stop. A clean stop prints `stopped: cancelled requests 0` and exits 0. Exit 1 when a received request had to be cancelled, a bound (`serving.http.shutdown_timeout_s`) was exceeded, or the address cannot be bound; exit 2 for a configuration refusal before any listener or model client exists.
- **Check:** `GET /health` answers; the port and your VRAM baseline are free after the stop (section 4); a request over `serving.max_concurrency + serving.queue_limit` gets 503 before any model call.

### 5.7 `eval run`

- **Needs:** the request read set, `evaluation.scenario_kinds`, `evaluation.split_by`, `evaluation.run.*` (`max_set_bytes`, `max_source_image_bytes`, `max_concurrency`, `max_sidecar_bytes`), `evaluation.judge.{blinding_seed,score_min,score_max,rubric_prompt_id}`, `storage.lock`, an active profile, the pipeline's servers up. `evaluation.run.max_concurrency` is a profile value (never 1 by default) bounding scenarios in flight in total; it does not guarantee free trace capacity, and a scenario the pipeline refuses for trace capacity is counted `not_run` (`trace_capacity`) and retried by the next invocation.
- **Run, the T-017 pilot (decision 172: retrieval on and off per scenario, about twice the model calls):**

```sh
python -m prompt_enhancer eval run --set <scenarios.parquet> --snapshot <snapshot-id> --variants both --split all --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

`--set` is the named scenario set (Parquet, the T-003 draft layout) inside the project root. `--snapshot` must equal the current published snapshot of `kb status`, and is required when any scenario runs with retrieval on and refused when none does (R-009). `--variants record` runs each scenario in the variant its record states; `both` also runs the retrieval-off variant of a retrieval-on record. `--split fit|test|all` selects scenarios; a pilot's results never fit a sentinel. `--source-images <dir>` is given exactly when an edit scenario runs with retrieval off: a directory of files named by the lowercase hex SHA-256 of the image (no extension), each hashing to its image id.
- **Prints** on success (every selected scenario has a durable result): the summary JSON: `run_id`, `units` (`total`, `skipped`, `selected`, `completed`, `failed`, `not_run`), `not_run_reasons`, `by_variant`, `structural`, `semantic` (`not_run`, reason "scoring contract not approved", until scores are imported), `intervals` (`not_run`), `packs` and `pack` (`pack_id`, `items`, `directory`).
- **Writes** under `<data_root>`: one row per scenario and variant in `eval/eval_results/`; the run directory `eval/runs/<run_id>/` with `manifest.json`, `summary.json`, `incomplete/` (one marker per failed or not-run unit), `packs/<16 hex digits of the pack id>/pack.jsonl` and `key.json`, `imports/`, and `run.lock`. The run id is a hash of the set, snapshot, configuration, models, prompts, profile, variants and split, so the same inputs resume the same run and a changed input is a new run.
- **Exit:** 0 every selected scenario has a result (the pack is written only then); 2 a refusal before any model call (a set that fails validation, a snapshot that is not the current one, a misplaced `--snapshot` or `--source-images`, another run holding the lock); 1 failed or not-run units, or a failure after the first written result: stderr reports the counts, the durable results stay, and you run the same command again after that process has ended (never while another `eval run` of this set is running).
- **Check:** `units.failed` and `units.not_run` are 0; `by_variant.*.envelope_status`; the structural literal counts. Metrics that need judgement and every confidence interval stay `not_run` here.

### 5.8 Scoring the judge pack, then `eval import`

The pack `<data_root>/eval/runs/<run_id>/packs/<16 hex digits of the pack id>/pack.jsonl` lists per scenario the prompt, the answer key, the rubric dimensions and the outputs under blind labels `A` and `B`, with no status, warning, retrieval state, trace id or variant. **The PM has it scored, not you** (decision 154): you paste the pack back or place it in shared storage, and the PM sends it to two subscription agents through its own worker tool. Decision 154 permits sending the T-003 scenario prompts, outputs, source images and retrieved knowledge-base content; content from a real deployment knowledge base beyond that scope goes out only after the user agrees. The scorers' brief states that the pack's text (prompts, knowledge-base and image text, outputs) is data to rate, never instructions (R-026). Two agents of different families, Claude and Gemini (decision 172), each score it **outside the product runtime** and write a score file in the closed schema [eval_judge_scores.v0](../contracts/schemas/eval_judge_scores.v0.schema.json): `schema`, `pack_id`, `scorer` (`id`, `family`, `model`) and `scores` (per scenario, ratings of `label`, `dimension`, `ref`, integer `value`). There is no free-text field. Give the scorers `pack.jsonl` only: `key.json` unblinds the labels and stays in the run directory. A single-output item (a retrieval-off record, or any run with `--variants record`) shows its own variant, so only a `both` pack compares retrieval on against off blindly.

```sh
python -m prompt_enhancer eval import --run-id <run-id> --scores data/scores/claude.json --scores data/scores/gemini.json --config configs/enhancer.deploy.yaml --overlay configs/overlays/deploy.yaml
```

- **Needs:** `evaluation.judge.{score_min,score_max,min_scorers,under_test_families,max_score_file_bytes}`, `evaluation.run.max_sidecar_bytes`, `storage.lock`; a finished run with a pack; at least `min_scorers` files (2 in the example), each of a different family and none from a family listed in `evaluation.judge.under_test_families`; score files inside the project root.
- **Prints:** `run_id`, `import_id`, `pack_id`, `evidence: "agent_judged"`, `pilot_only: true`, `scorers`, `disagreements`, `needs_user_review`, `aggregates`, `disagreement_list`. Writes `eval/runs/<run_id>/imports/<id>.json` and refreshes `summary.json`; never writes `eval_results`.
- **Exit:** 0, also when the scorers disagree (every difference is flagged, there is no threshold); 2 for an unknown, duplicate or missing scenario or rating, a value outside the scale, a field outside the schema, one family twice, or a run still in use by `eval run`; 1 a failure after a side effect.
- **Check:** `needs_user_review`; scores are pilot evidence only and never reach thresholds, calibration or `eval fit`.

### 5.9 Commands that refuse in this build

| Command | Exit | Why | Owner |
| --- | --- | --- | --- |
| `eval build` | 2 | scenario generation is not part of this tool | T-003 |
| `eval fit` | 2 | fitting `fit_from_*` values is not part of this tool | T-030 |
| `kb sync --only <folder>` | 2 | a scoped sync would treat unselected folders as removed | none (not planned) |

```sh
python -m prompt_enhancer eval build --config configs/enhancer.deploy.yaml
python -m prompt_enhancer eval fit --config configs/enhancer.deploy.yaml
python -m prompt_enhancer kb sync --only <folder> --config configs/enhancer.deploy.yaml
```

Each prints `refused: ...` on stderr and nothing on stdout. Do not use them as part of a first deployment.

## 6. A first deployment, in order

1. Create the profile and the overlay (section 3). `kb status` reads only the storage keys, so it checks those first; every other sentinel is reported by the command that reads it (section 3 table), so expect to fix them one by one, command by command. A sentinel with an unknown owner is not yours to guess: ask the PM.
2. Record the VRAM baseline (section 4). Start the embedding and VLM servers you need, from their own `--help`.
3. `kb sync --plan`, read the guard and the work, then `kb sync` (with your go), then `kb status` (5.1, 5.2). Expect the one full recompute of 5.2 if an older store exists.
4. Place the admin's profile folder with its `system_prompt.txt` (not edited), `profile register`, the admin confirms the manifest, `profile check` (5.4).
5. `enhance once` smoke: a generate and an edit request, each with retrieval on and with retrieval off (a copy of your profile that sets `rag.enabled: false`, which needs no knowledge base at all; no request field switches retrieval) (5.5).
6. `enhance serve`: `GET /health`, one `POST /v1/enhance`, then Ctrl+C and confirm exit 0 and VRAM at baseline (5.6).
7. `eval run --variants both` on the named set and snapshot for the T-017 pilot (5.7).
8. Hand the pack to the PM, who has it scored by Claude and by Gemini (decision 154; real knowledge-base content beyond its scope only after the user agrees); place the two score files the PM returns under `data/scores/`, then `eval import` (5.8).
9. Stop every server you started; VRAM back at the baseline.

**Resume and rollback.** `kb sync` and `eval run` resume by content identity: run the same command again after the first process has ended. `kb rollback <snapshot-id>` returns to a retained snapshot; `kb gc` collects what retention allows.

## 7. What to paste back

The commands you ran with their exit codes; the JSON each printed (the stderr line when one failed); `kb status` before and after; the VRAM lines of section 4 before and after each GPU job; the versions of the model servers and the pinned model revisions; for `enhance once` and `enhance serve`, the envelope `status` and `warnings`, not the prompt text if it is private; for the pilot, `summary.json` and the import report. Never paste anything from `.secrets/`.

## 8. Not run here

Only `kb sync` has run against live local models, once, on the dev GPU over a one-folder smoke knowledge base (section "State of the evidence"); no other command has run live and none has run on the deployment; no latency, throughput, VRAM or quality figure exists. The server start commands, `curl` use against `enhance serve`, and the `nvidia-smi` lines are documented usage, not run by the agent. Live, dev-GPU, deployment and performance runs belong to T-017 and to you; fitted values belong to T-030.
