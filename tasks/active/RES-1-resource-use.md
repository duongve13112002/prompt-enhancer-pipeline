# RES-1: servers at capacity, no barriers, no repeated work (decision 149, S46)

Status: REVIEWING; Fix round 1 and approved pre-encode scope extension implemented and verified, awaiting independent PM review/integration. Base `ed69118`. R-IDs: R-015, R-017, R-018, R-020.

The rule (decision 149, mandatory):
- Keep model and embedding servers busy up to their configured capacity, with concurrent requests and no stage-wide barrier.
- Remove repeated or serialized work first.
- Never trade accuracy for speed.
- Back off only on real 429, overload or OOM signals.
- Prove every faster path equal to the serial one with a differential test, including failure and stop cases. Logs show requests in flight and idle gaps.
- Shared-machine limits still hold.

## Step 0

Builder: Claude Opus, 2026-11-17. Base `ed69118`. No source edit yet. The probe scripts were scratch under `.tmp/RES-1/probe/` and have been deleted; their numbers are copied below.

### Unknowns

- Whether "no stage-wide barrier" also means pipelining across stages (K3 to K4 to K1-text per concept), or only inside each stage (Q2).
- Whether to drain or cancel requests already in flight on a fatal error or a stop (Q3).
- Whether a retried `httpx.ReadTimeout` counts as a real overload signal (Q4).
- The brief quotes 27 schemas and about 50 s per registry build; measured 26 schemas and about 0.6 s (below). `assembly.py`, the T-014 P2 cache, does not exist at this base.

### Measured evidence (experiment; CPU only; labelled async `httpx.MockTransport` doubles; `max_concurrency=4`)

Probe run through `test_slot.py`, `3 passed in 6.90s`:

| Probe | Cap | Peak in flight | Requests | Span s | Idle slot-s (cap minus in-flight, integrated) |
|---|---|---|---|---|---|
| K4 `build_aliases`, 12 concepts, one at 0.5 s, rest 0.05 s | 4 | 4 | 12 | 0.672 | 1.454 of 2.69 (54 % idle: wave barrier) |
| K2 `name_folders`, 8 image folders, 0.05 s each | 4 | **1** | 8 | 1.891 | 7.065 of 7.56 (93 % idle: fully serial) |
| `SchemaRegistry(contracts/schemas)` x3 | - | - | - | 0.698, 0.580, 0.630 | 26 schemas, `check_schema` on each |

### Class sweep (source-verified line references at `ed69118`)

| # | Path | In flight vs configured capacity | Barrier | Repeated work | Proposed fix | Differential test |
|---|---|---|---|---|---|---|
| 1 | K1 images `k1_embed.py:407-439` | up to `max_concurrency` batch requests per window (`_window` :318-320; transport semaphore `model_transport.py:52`) | **per window**: read, verify and decode of the next window wait for every batch of this one (:408-427); decode runs on the event-loop thread, so the server idles during decode | none in-path; **batch composition depends on `max_concurrency`** (batches are cut per window, :425), and T-018 measured bf16 batch-size variance (`T-018-embedding-server.md:44`) | sliding pipeline: decode in a pool of `runtime.cpu_workers` threads, results kept in sorted order, fixed consecutive batches of `embedding.batch_size` over decodable items (independent of concurrency), at most `max_concurrency` batches in flight, prepared images bounded by `batch_size*max_concurrency`; checkpoint every success before re-raising a fatal error (today :311-314 raises before any checkpoint of the window) | cap 1 vs 4 and cpu_workers 1 vs 3, with undecodable, changed-source and HTTP-error items interleaved: same vectors, records, sync_state, failure set; fatal mid-run: every received batch committed |
| 2 | K1 concept texts `k1_embed.py:486-502` | as row 1 | per window (:487-489) | none | same sliding flow (no decode) | cap 1 vs 4, mid failure, fatal stop |
| 3 | K2 naming and assessment `k2_naming.py:559-638` | **1**, whatever `max_concurrency` (sequential `await evaluate` :577, :617; probe above) | every folder waits for the previous one | `_validate_reply` builds a validator per call (:44, cheap; `_schema` is stat-cached `config.py:381-407`) | per-folder flow bounded by the larger `max_concurrency` of the two role backends (the transport still enforces each backend); **draws stay in folder order** through an ordered gate so a seeded `rng` consumes the same values as serial; post-call row and report updates applied in folder order (`_replace_members` :436 is first-writer-wins on `display_name`) | cap 1 vs 4, random latencies, a usable, an unusable, an HTTP-failed and a schema-invalid folder: same rows, report, draws, sync_state; fatal stop |
| 4 | K2 observation `k2_observe.py:300-314` | up to cap per **wave** | wave barrier: each batch of `max_concurrency` waits for its slowest (:302-309) | facet-group enum read per call (:277, cached, cheap) | per-item flow (shared helper, see plan) | cap 1 vs 4, mid failure, fatal stop; report lists ordered |
| 5 | K3 cards `k3_cards.py:330-340` | up to cap per wave | wave barrier | observations filtered per concept, O(concepts x observations) (:262) | per-item flow; group observations by concept once | cap 1 vs 4, mid failure, fatal stop |
| 6 | K4 aliases `k4_aliases.py:209-220` | up to cap per wave (probe: 54 % slot idle) | wave barrier | none | per-item flow | cap 1 vs 4, mid failure, fatal stop |
| 7 | S1 dense query embedding `candidates.py:181-186` | **1** (sequential batches) | each batch waits for the previous one | none | send all query batches concurrently (bounded by the embedder transport), merge hits in batch order | cap 1 vs 4: identical candidate list and scores |
| 8 | S2 planner `planner.py:296`, second chance :348-351 | planner call 1 by design (each `ask` depends on the previous reply); second-chance S1 lookups **1** (sequential per mention) | per mention | **`SchemaRegistry` rebuilt per `plan` call** (0.6 s measured) | cached registry (row 10); concurrent second-chance lookups, results merged in mention order | same plan, warnings and calls, cap 1 vs 4 |
| 9 | Per-stage model runs `kb_sync.py:136-160`, `k2_naming.py:521`, `k2_observe.py:269`, `k3_cards.py:283`, `k4_aliases.py:184` | - | - | with no injected client each of four stages builds its own `ModelClientRun`: a new registry (0.6 s), **a new probe per backend**, and **a new budget ledger**, so a cloud `call_budget` grant applies per stage, not per sync (`model_client.py:117-121`) | `sync_knowledge` owns one run for all model stages (Q5) | flow test: one probe per backend per sync; budget shared |
| 10 | Schema registry `model_schemas.py:11-24`, used at `model_client.py:121`, `planner.py:296` | - | - | 26 `check_schema` calls per build | process cache keyed by the resolved directory plus (name, `mtime_ns`, size) of every `*.schema.json`; any change, addition or removal rebuilds; a build failure is never cached; `get` returns a copy so no caller can mutate the shared schema | same validate results and digests; edit or add a file: rebuilt; invalid file: refused each time |
| 11 | Transport retries `model_transport.py:52-70` | semaphore held through back-off, so a backing-off request takes one slot and gives it back afterwards | - | retries **any** `httpx.TransportError`, including read and write timeouts, which are not overload signals and resend a request the server may still be running (and on cloud spend again) | retry only connect-phase errors plus configured `retry_statuses` (Q4) | a ReadTimeout is not retried; ConnectError is; 429 is |
| 12 | embed_server `embed_server/server.py:375-402` | admits `server.max_pending_requests`; one GPU lock, one request per forward; parse and decode run in handler threads outside the lock, so they overlap the GPU | none inside the server | model loaded once (`_State.engine`); no repeated setup found | **no change**: merging requests into one forward changes the batch and therefore bf16 vectors (accuracy). Client `max_concurrency` above `max_pending_requests` produces self-inflicted 503s (no cross-check today, `config.py:1113` checks only connections) (Q4) | n/a |
| 13 | Image decode and resize | K1 decodes each image once per window pass; K2 naming and observer decode per call inside `model_client.py:431` | - | the same image is decoded again in K2 with role image settings | **no change**: role settings may differ from K1's, and keeping prepared bytes across stages costs memory; within one call path nothing is decoded twice | n/a |

Requests in flight and idle gaps are not logged anywhere today (no `logging` in `src/`).

### Configuration impact

No new key. Existing keys whose effect changes:

| Key | Today | After | Override test (non-default) | Refusal test |
|---|---|---|---|---|
| `vlm.backends.<b>.max_concurrency` | bounds transport; wave size in K2-observe, K3, K4; window in K1 | per-item flow cap in K1, K2 naming (new), K2 observe, K3, K4, S1 dense (new), S2 second chance (new) | per path: cap 3 with at least 9 items gives a peak in flight of exactly 3 (counting double) | 0, `true`, sentinel refused at load and at standalone stage entry |
| `runtime.cpu_workers` | K0 scan workers | also the K1 decode pool size (already in the `kb sync` read set, `config.py:59`) | 3 workers: peak concurrent decodes 3 | 0 refused at K1 entry |
| `embedding.batch_size` | batch size, cut per window | batch size, consecutive over decodable items, independent of concurrency | 3 with 7 items, 1 undecodable: batches [3,3] | existing loader refusal |
| `vlm.backends.<b>.http.*` (retries) | unchanged unless Q4 is accepted | | | |

Retained literals added: report keys of the new capacity record (`protocol-bound`). Telemetry goes into each stage report as `report['capacity'][<backend>]`: `max_concurrency`, `requests`, `peak_in_flight`, `busy_s`, `idle_gap_s` (time with nothing in flight between the first and last request), `below_capacity_slot_s`. The differential tests compare everything except this timing record.

**Checkpoint keys (R-017):** every K1 to K4 key includes `tool_revision` over the source text of the stage module and `MODEL_TOOL_MODULES` (`k1_embed.py:48-49, 144-146`; `k2_naming.py:285`; `k2_observe.py:142`; `k3_cards.py:246`; `k4_aliases.py:148`), and `sync_identity` includes it too (`kb_sync.py:65-67`). Any RES-1 edit to these files therefore re-keys every K1 to K4 checkpoint and re-runs every offline stage on the next `kb sync`. Stage outputs are unchanged except K1 batch composition, which already differed between caps when decode failures were interleaved.

### Questions (ranked, each with a proposed answer)

1. **Full re-run of offline stages.** Any edit re-keys all K1 to K4 checkpoints (above), which costs GPU time and, on a cloud backend, granted requests. *Proposed:* accept, and land RES-1 as a single integration so the deployment knowledge base re-runs once. Narrowing tool identity to exclude scheduling code would be a separate decision.
2. **Scope of "no barrier".** *Proposed:* RES-1 removes every barrier inside a stage (rows 1 to 8). Pipelining across stages, per concept from K3 to K4 to K1-text and per folder from K1 to K2 naming, goes to a follow-up task with its own Step 0. Two reasons: K2 observation's near-duplicate grouping is a real global dependency (`k2_observe.py:189`), and `require_success` (`kb_sync.py:26-30`) would change.
3. **Fatal error and stop.** *Proposed:* on a fatal item error, admit nothing new, let in-flight requests finish and checkpoint their answers, then raise the first error, so already-paid answers are kept. On an external cancel (stop, Ctrl-C), cancel in-flight requests at once and await them, so a stop stays prompt. An answer not yet received is lost, and the ledger already counts a sent cloud request as today.
4. **Back-off signals.** *Proposed:* retry only connect-phase transport errors (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) plus the configured `retry_statuses`; read and write timeouts fail the item. Also refuse at load an `embed_server` embedder whose `max_concurrency` exceeds `server.max_pending_requests`, since the excess requests are guaranteed 503s. Both are validation and behaviour changes, not new keys.
5. **One model run per `kb sync`.** *Proposed:* yes: one probe per backend per sync, one registry, and one budget ledger, so a cloud grant is spent per sync rather than once per stage (up to 4x today). This changes spend semantics, so the PM should confirm with the user.

### Plan (checkable finish)

1. `model_resources.py`: a metered `BackendConcurrency` that records in-flight, peak, busy, idle-gap and below-capacity time, plus one shared `flow(items, worker, limit)` helper. The helper runs items one by one with at most `limit` in flight, returns results in item order, and implements Q3's drain on a fatal error and cancel on a stop. Property tests (Hypothesis) over latencies, failure positions and cancel points show results and committed set equal to `limit=1`.
2. The cached schema registry (row 10), used by `ModelClientRun` and `planner.plan`, with equality and invalidation tests, and the timing before and after recorded.
3. Stages on `flow`: K2 naming (ordered draw gate, ordered apply), K2 observe, K3 (pre-grouped), K4; K1 image and text sliding pipeline (row 1); S1 dense and S2 second chance; the stage `report['capacity']`; and, per the answers, rows 9 and 11.
4. Tests, one file per pytest process through `test_slot.py`: the test files of each changed module, the new differential and override tests, then `tests/test_kb_sync_flow.py` once. Run 3 to 10 mutants per changed path on scratch copies. Run `ruff check` and `ruff format --check`. Write the Configuration audit row by row.

**Finish when:** every sweep row 1 to 8 (plus 9 and 11 if approved) has a passing differential test, equal to cap 1, that covers a mid failure and a stop. The counting double shows peak in flight equal to the configured cap (3) on every changed path. Re-running the probe gives K2 naming a peak of 4 and K4 idle slot-seconds below the wave figure above. Registry construction happens once per process while the files are unchanged. Lint and format output are quoted, and the Configuration audit has every row.

## PM answers to Step 0 (2026-11-17; each a "PM decision pending user review" unless marked)

- Plan approved, in two parts. **Part A now:** plan steps 1 and 2 only (`model_resources.py` with `BackendConcurrency` and `flow` and their property tests; the cached schema registry in `model_schemas.py` with equality and invalidation tests, used from `planner.plan`; `config._cached_schema` fixed so the key and the value come from one snapshot, with concurrent-writer tests in-process and in a separate process). `ModelClientRun` is not wired yet. **Part B** starts after the PM applies the KBS-F1 patch (which edits `model_client.py`, `model_transport.py`, `kb_sync.py` and K2 to K4) and messages me.
- Q1 accepted: one single integration; the dev knowledge base re-runs once; tool identity is not narrowed.
- Q2 accepted: RES-1 removes the barriers inside stages (rows 1 to 8). Cross-stage pipelining is queued as RES-2 (rows handed off under `Noticed, not done`).
- Q3 accepted: on a fatal error, admit nothing new, drain in-flight requests, checkpoint every answer received, then raise the first error. On an external stop, cancel at once and await.
- Q4 accepted, Part B: retry only `ConnectError`, `ConnectTimeout`, `PoolTimeout` plus the configured `retry_statuses`; read and write timeouts fail the item; refuse at load an `embed_server` embedder whose `max_concurrency` exceeds `server.max_pending_requests`.
- Q5: row 9 belongs to KBS-F1 (decision 148, accepted). It is not implemented here. In Part B I verify that one probe per backend, one registry and one ledger hold per sync, and add the flow test if it is missing.
- The capacity record `report['capacity'][<backend>]` is accepted as protocol-bound report keys; the differential tests exclude its timing fields.

## Part A: Configuration impact (filled before the first runtime edit)

No profile key is read, added or changed in Part A. `BackendConcurrency` keeps its `limits` argument (callers pass `vlm.backends.<b>.max_concurrency`, unchanged). The `flow` limit is a caller argument; stages pass the configured `max_concurrency` in Part B. The schema caches have no setting. The per-file cache keeps the existing `config.SCHEMA_CACHE_CAPACITY` (128, `safety-bound`, unchanged). The registry cache has its own `model_schemas.REGISTRY_CACHE_CAPACITY` (16 directories), because a registry holds all 26 parsed schemas. New literals: `REGISTRY_CACHE_CAPACITY` (`safety-bound`: in-process memory cap, not a deployment choice) and the capacity record keys `CAPACITY_FIELDS` and `TIMING_FIELDS` (`protocol-bound`, PM answer 6).

## Part A: completion report (2026-11-17)

**Behaviour**
- `model_resources.py`:
  - `BackendConcurrency` keeps one semaphore per backend and now meters it. `for_backend(name)` returns an async slot that counts in-flight requests once held.
  - `open_window(name).close()` returns the capacity record `{max_concurrency, requests, peak_in_flight, busy_s, idle_gap_s, below_capacity_slot_s}`, integrated from the first to the last event in the window.
  - The `clock` argument is a seam for labelled tests.
  - New `flow(items, worker, limit)`: per-item flow with at most `limit` in flight; results come back in item order.
  - On a worker error, `flow` admits nothing new, drains running items, then raises the error of the earliest failed item in item order (Q3). On an external stop it cancels running items at once and awaits them.
  - An error raised by `worker(item)` before it returns its awaitable fails that item.
- `model_schemas.py`:
  - New `SnapshotCache`: a thread-safe LRU keyed by the SHA-256 of the exact bytes the value was built from, with every length framed. On a miss it builds from those bytes, re-reads, and publishes only if the bytes are unchanged. A failed build publishes nothing.
  - New `cached_registry(directory)`: one registry per unchanged directory, used by `planner.plan`. A changed, added or removed file triggers a rebuild.
  - `SchemaRegistry.get` returns a private copy of the schema.
  - `SchemaRegistry.from_snapshot` builds from bytes already read; `SchemaRegistry(directory)` still reads the directory itself.
- `config._schema`: replaced the `lru_cache` keyed by `(path, mtime_ns, size)`, whose key came from a stat taken before the read (the T-014 P2 split), with `SnapshotCache.fetch(path.read_bytes, _parsed_schema)`. The key and the value now come from one read. `functools.lru_cache` is no longer imported.
- `ModelClientRun` and the stages are not wired (Part B).

**Measured** (CPU; experiment):
- Uncached `SchemaRegistry` build: 0.314 s mean of 3; cached hit (read and hash 26 files): 0.00157 s mean of 50.
- `config._schema` hit: 0.00076 s for the 208 KB file, 0.0006 s for a small one. The existing `_schema_path` containment check accounts for 0.00065 s of that; the added read is 0.00004 s, against 0.000005 s for the stat it replaces.
- Race coverage of the writer tests (scratch run): every mode saw both versions and refused half-written files (9 to 51 refusals). Of 17 to 56 builds, only 1 or 2 were published; the rest saw a change during the build.

**Commands and output** (each through `tools/agent-workflow/test_slot.py`, one file per pytest process, `--basetemp=.tmp/RES-1/bt` deleted after each):

| Test file | Result |
|---|---|
| `tests/test_model_resources.py` | 21 passed in 1.16s |
| `tests/test_model_schemas.py` | 14 passed in 1.59s |
| `tests/test_fix01.py` | 60 passed, 1 skipped in 5.61s (existing symlink skip) |
| `tests/test_planner.py` | 67 passed in 11.80s |
| `tests/test_planner_config.py` | 26 passed in 1.58s |
| `tests/test_model_client.py` | 78 passed in 13.93s |
| `tests/test_model_client_image_side.py` | 36 passed in 7.27s |
| `tests/test_model_transport.py` | 35 passed in 3.73s |
| `tests/test_model_cloud.py` | 10 passed in 0.13s |
| `tests/test_loader.py` | 134 passed in 14.94s |
| `tests/test_k1_embed.py` | 68 passed, 1 skipped in 16.71s |
| `tests/test_kb_sync_flow.py` | 28 passed in 123.43s |

**New tests**
- `tests/test_model_resources.py`:
  - Capacity window integration, checked exactly with a fake clock.
  - A window opened mid-flight, and a closed window that records nothing more.
  - Override test: the peak in flight equals the limit, for limits 1 and 3.
  - A cancelled wait for a slot is not counted.
  - The `flow` limit is refused for 0, -1, `True`, 1.5 and `None`.
  - No wave barrier: one slow item, and every other item finishes first.
  - A stop cancels at once and does not drain.
  - Empty input, and an error raised at admission.
  - Hypothesis, 150 examples: `flow` at any limit equals `limit=1` (the same results or the same first error). It keeps every answer the serial run keeps, never exceeds its limit, admits nothing after the first failure and cancels nothing.
  - Hypothesis, 100 examples: a stop at a random point leaves no task behind, cancels every started item that has not finished, and admits nothing afterwards.
- `tests/test_model_schemas.py`:
  - The cached registry equals the uncached one and is built once.
  - Rebuild on change, addition and removal.
  - An invalid schema is refused on every call and never published.
  - Callers get copies.
  - A registry changed during its build is used once and never published; the same check runs for `config._schema`.
  - A same-size rewrite with the mtime restored is seen. This is the old split; it fails with a stat key.
  - Concurrent writer in a thread and in a separate process (`CREATE_NO_WINDOW`, `-X faulthandler`), for both caches: every read is a valid version or a refusal, every published entry's value matches its key's bytes, and after the writer stops the final version is served.
- `tests/test_fix01.py`: the existing cache test was moved to the new cache API (`_SCHEMA_CACHE.clear()`, `.hits`).

**Mutants** (10, on scratch copies of one module each, loaded by a scratch pytest plugin; the scratch has been deleted):

| Mutant | Result |
|---|---|
| `len(running) < limit` → `<=` | killed |
| keep admitting after an error | killed |
| raise the error with the largest index instead of the smallest | killed |
| drain instead of cancelling on a stop | killed (needed the new test `test_flow_stop_does_not_wait_for_running_items`) |
| `delta > 0` → `>= 0` | **survived: equivalent** (`delta` is only +1 or -1) |
| busy and idle swapped | killed |
| always publish (no re-read) | killed |
| `get` without a copy | killed |
| bytes keyed by length | killed |
| build from a second read | killed |

**not_run**
- `ruff check` and `ruff format --check`: ruff is not installed in `.venv` (`No module named ruff`), and installs are not allowed. pyflakes and flake8 are also absent. I ran `py_compile` on the 7 changed files (they compile) and reviewed imports by hand.
- Part B tests: not started, by design.

**Configuration audit (Part A)**

| Item | Class | Evidence |
|---|---|---|
| `vlm.backends.<b>.max_concurrency` | `configurable`, existing | Still the only source of `BackendConcurrency` limits (callers unchanged). Override test: peak equals the limit (1 and 3). Refusal tests: 0, -1, `True`, 1.5 refused (existing plus new `flow` refusal). |
| `SCHEMA_CACHE_CAPACITY` (128) | `safety-bound`, existing, unchanged | `config.py:24` |
| `REGISTRY_CACHE_CAPACITY` (16) | `safety-bound`, new | `model_schemas.py`; in-process memory cap |
| `CAPACITY_FIELDS`, `TIMING_FIELDS` | `protocol-bound`, new | PM answer 6; the field order is pinned by a test |
| new profile keys | none | |

**Decisions and deviations** (each a "PM decision pending user review")
- PM answer 3 says "raise the first error". `flow` raises the error of the earliest failed item in item order, not in time order. That choice never depends on timing and equals the error the serial run raises; the other item errors stay recorded by their own workers.
- `SchemaRegistry.get` now always returns a deep copy, including for uncached registries, so a shared registry cannot be mutated.
- The registry cache has its own capacity, 16, instead of reusing 128.
- Content-keyed entries are shared across paths with identical bytes, which is harmless because the value does not depend on the path.
- Re-keying (R-017): `model_resources`, `model_schemas` and `config` are in `MODEL_TOOL_MODULES` and in the K1 tool revisions, so Part A alone already changes every K1 to K4 checkpoint key. This is accepted under Q1 (single integration).

**Changed files**
- `src/prompt_enhancer/model_resources.py`, `src/prompt_enhancer/model_schemas.py`, `src/prompt_enhancer/config.py`, `src/prompt_enhancer/planner.py`
- `tests/test_model_resources.py`, `tests/test_model_schemas.py`, `tests/test_fix01.py`
- this task file

**Noticed, not done**
- **RES-2 handoff (cross-stage pipelining, decision 149, PM answer 2):**
  - Per concept, K3 card → K4 aliases → K1 concept-text embedding: today each waits for the whole previous stage (`kb_sync.py:151-160`).
  - Per folder, K1 image → K2 naming: naming needs only the folder's own `decode_status` (`k2_naming.py:592-593`), yet waits for all of K1 (`kb_sync.py:136-141`).
  - A real global dependency: K2 observation's near-duplicate grouping needs every K1 vector (`k2_observe.py:189`), so K1 → K2 observation stays a barrier unless grouping becomes incremental.
  - `require_success` (`kb_sync.py:26-30`) makes any failed item fail the sync before the next stage starts. Pipelining would issue later-stage calls for other items first; their answers are checkpointed and reused on rerun, but spend happens before the failure is known. That semantics needs a decision.
- My `py_compile` run left 7 git-ignored `.pyc` files in `src/prompt_enhancer/__pycache__/` and `tests/__pycache__/`. The shell guard blocks deleting them outside `.tmp/`; the PM may remove them.
- `tests/test_model_schemas.py:34` is a pre-existing 125-character line, left as is.

## Part B: Configuration impact (filled before the first Part B runtime edit; base: Part A plus KBS-F1 up to `dcf2320`)

No new key.

| Key | Effect after Part B | Override test | Refusal test |
|---|---|---|---|
| `vlm.backends.<role backend>.max_concurrency` | `flow` limit in K2 naming (new; the larger of the two role backends), K2 observe, K3, K4; per-backend transport bound (unchanged) | peak in flight equals 3 with cap 3 in each stage | 0 refused at load (existing) and by `flow` |
| `vlm.backends.<embedder>.max_concurrency` | K1 image and text: up to cap requests in flight plus one chunk decoding ahead (`flow` limit cap + 1); S1 dense batches and S2 second-chance lookups concurrent, bounded by the embedder transport | peak 3 with cap 3 (K1, S1) | existing; **new**: an `embed_server` embedder with `max_concurrency` above `server.max_pending_requests` is refused at load (Q4) |
| `runtime.cpu_workers` | also the K1 image decode pool size (resolved by `k0.resolve_workers`, `auto` included) | 3 workers: 3 decodes at once | 0 and a sentinel refused at K1 entry |
| `embedding.batch_size` | K1 batches are consecutive chunks of `batch_size` pending inputs (undecodable or changed ones are left out of their chunk), independent of concurrency | cap 1 vs 4 give the same batches | existing |
| `vlm.backends.<b>.http.retry_statuses`, `max_retries`, back-off | retries on configured statuses unchanged; transport errors retried only for `ConnectError`, `ConnectTimeout`, `PoolTimeout` (Q4) | a `ConnectError` is retried; a `ReadTimeout` and a `WriteTimeout` are not | n/a |

Retained literals: `RETRYABLE_TRANSPORT_ERRORS` (`protocol-bound`: httpx exception classes for the connect phase).

### Part B progress log (for continuity after compaction)

- The Part A test files rerun on the combined tree: all pass, unchanged except `test_model_client` (79 tests, KBS-F1 added one).
- Done:
  - `model_resources`: `OrderedTurns`, `CapacityLog`, `BackendConcurrency.names`.
  - `model_transport`: `RETRYABLE_TRANSPORT_ERRORS`.
  - `model_client`: `cached_registry`, and `run_concurrency(client)`.
  - `assembly`: `cached_registry`.
  - `config`: embed_server `max_concurrency <= server.max_pending_requests` (in `validate_embedder_reads`, plus `EMBED_SERVER_CLIENT_READS`).
  - `k1_embed`: chunked `flow`; decode in a `ThreadPoolExecutor(runtime.cpu_workers)`; flow limit cap+1 for images; `report['capacity']`.
  - `k2_observe`, `k3_cards` (pre-grouped observations), `k4_aliases`: `flow` and `report['capacity']`.
  - `k2_naming`: `name_folder` coroutine effects applied in folder order, `OrderedTurns` around `_draw`, capacity.
  - `candidates._dense` concurrent batches; `planner` second-chance concurrent lookups.
  - Tests written: `tests/test_stage_flow.py` (differential per path), transport retry classes, embed_server
    admission refusal (`test_embed_server_read_set`), Q5 one-run assertion (`test_kb_sync_production_probes`).
  - Test updates forced by the new semantics: `test_k1_embed` resume test (drain), `test_k4_aliases` double gains
    `concurrency`, `test_k2_observe_edges` fatal test now asserts drain, `test_assembly*` drop the test-only shared
    registry double (production caches now; F1 race test rewritten against `_read_directory`),
    `test_embed_server_read_set` moves `max_pending_requests` from UNREAD to READ.
- Done: all test runs, mutants and the completion report below.

### Part B decisions (each a "PM decision pending user review")

1. `in_flight_slot_s` replaces Part A's `below_capacity_slot_s`: `max_concurrency * elapsed` overflows a float for an
   exact profile integer above the float range; the unused capacity is left to the reader (docstring formula).
2. K1 image flow limit is `max_concurrency + 1`: one chunk reads and decodes while `max_concurrency` requests are in
   flight; memory bound `batch_size * (max_concurrency + 1)` prepared images.
3. K1 batches are consecutive `batch_size` chunks of pending inputs; an undecodable or changed image is left out of its
   chunk's request (a request can be smaller). Composition depends only on inputs and `batch_size` (bf16, T-018).
4. K2 naming: model calls concurrent, report and concept updates collected as per-folder effects and applied in folder
   order; seeded draws taken in folder order through `OrderedTurns`; flow limit is the larger `max_concurrency` of the
   two role backends (each transport still bounds its own backend).
5. S2 second chance: all lookups of a round are admitted at once (`flow` limit `len(mentions)`); the shared embedder's
   transport bounds requests in flight.
6. `server.max_pending_requests` joins K1's read set for an `embed_server` embedder (read, not hashed): kb sync, K1
   standalone, dev-compare and S1 dense now refuse a sentinel there.
7. Tests that asserted cancel-on-fatal now assert drain-on-fatal (PM answer 3).
8. The test-only shared-registry double of T-014 P2 (`assembly_fixtures.shared_registry`) is removed: production now
   caches per unchanged bytes, and the F1 race test runs against the cache (`_read_directory` raced) instead.

## Part B: completion report (2026-11-17)

**Behaviour** (R-015, R-017, R-018; decision 149)
- Per-item `flow`, no barrier inside a stage, each bounded by the configured `max_concurrency`:
  - K1 images (`k1_embed.py:392-486`): consecutive `embedding.batch_size` chunks of pending images; each chunk reads,
    verifies and decodes in a `ThreadPoolExecutor(runtime.cpu_workers)`, then sends one request; flow limit
    `max_concurrency + 1` (one chunk decodes ahead). K1 texts (`:489-550`): chunks with limit `max_concurrency`.
  - K2 naming (`k2_naming.py:586-692`): folders run concurrently; seeded draws keep folder order through
    `OrderedTurns`; report entries and new concept rows are collected per folder and applied in folder order.
  - K2 observation, K3 cards, K4 aliases: `flow` over the sorted items (`k2_observe.py:307`, `k3_cards.py:339`,
    `k4_aliases.py:216`); K3 groups observations by concept once (`k3_cards.py:252-254`).
  - S1 dense batches (`candidates.py:182-205`) and S2 second-chance lookups (`planner.py:350-363`) go out together;
    results merge in batch or mention order.
- Fatal error: nothing new admitted, running items drained and checkpointed, then the earliest failed item's error.
  External stop: running items cancelled at once and awaited (PM answer 3).
- Retries (PM answer 4): only `ConnectError`, `ConnectTimeout`, `PoolTimeout` (`model_transport.py:12`) plus the
  configured `retry_statuses`; a read or write timeout fails the item.
- Load-time refusal: an `embed_server` embedder whose `max_concurrency` exceeds `server.max_pending_requests`
  (`config.py:991-996`), for every caller of `validate_embedder_reads` (kb sync, K1 standalone, dev-compare, S1 dense).
- Requests in flight are visible: every K1 to K4 report has `report['capacity'][<backend>]` =
  `{max_concurrency, requests, peak_in_flight, busy_s, idle_gap_s, in_flight_slot_s}`.
- Setup cached: `ModelClientRun.schemas` and `assembly` use `cached_registry` (Part A cache).
- Q5 (source-verified plus test): kb sync creates one `ModelClientRun` when no client is given (`kb_sync.py:134`),
  so one probe per backend, one registry and one ledger; `test_kb_sync_production_probes` now asserts one run and at
  most one registry build per sync (8 probe requests, unchanged). Row 9 itself is KBS-F1's, not changed here.
- No accuracy knob lowered; no new configuration key.

**Measured** (experiment, CPU, labelled async HTTP doubles with seeded 1-20 ms delays, `max_concurrency` 3): every
path reached peak in flight 3 against 1 for the serial run (asserted in `tests/test_stage_flow.py`). No wall-clock
speed-up is claimed; the user's backend decides it (VLM rule 12).

**Commands and output** (one file per pytest process through `tools/agent-workflow/test_slot.py`, no `-n`,
`--basetemp=.tmp/RES-1/bt` removed after each; Part A files first rerun on the combined tree: all pass, unchanged
except `test_model_client` 79)

| Test file | Result |
|---|---|
| `tests/test_stage_flow.py` (new) | 18 passed in 21.9s; after the last edit the whole file ran twice, green both times |
| `tests/test_model_resources.py` | 21 passed |
| `tests/test_k1_embed.py` | 68 passed, 1 skipped |
| `tests/test_k1_config.py` | 558 passed |
| `tests/test_k2_config.py` | 310 passed |
| `tests/test_k2_fix_round1.py`, `_round2`, `_round3` | 39, 42, 35 passed |
| `tests/test_k2_name_override.py` | 47 passed |
| `tests/test_k2_observe_config.py`, `_edges`, `_properties`, `_shared_config`, `_shared_keys` | 186, 20, 4, 133, 6 passed |
| `tests/test_k2_p3_notes.py`, `_probe`, `_properties`, `_r026`, `_semantic_keys` | 34, 18, 21, 3, 212 passed |
| `tests/test_k3_*` (config, edges, fix rounds 1-2, properties) | all passed (edges 11) |
| `tests/test_k4_aliases.py`, `_config`, `_properties` | 44, 273, 22 passed |
| `tests/test_candidates.py`, `_config`, `_properties` | 34, 147, 9 passed |
| `tests/test_planner.py`, `_config` | 67, 26 passed |
| `tests/test_assembly.py`, `_config`, `_properties` | 45, 18, 15 passed |
| `tests/test_pipeline_s0_s3.py` | 1 passed |
| `tests/test_model_transport.py` | 42 passed |
| `tests/test_model_production_probes.py`, `test_model_schemas.py`, `test_model_cloud.py`, `test_model_client_image_side.py` | 84, 14, 10, 36 passed |
| `tests/test_kb_sync_production_probes.py`, `_diagnostics`, `_gates`, `_invalidation` | 1, 10, 19, 26 passed |
| `tests/test_embed_server_read_set.py`, `_config`, `_http`, `_protocol`, `_reference` | 92, 160, 39, 89, 149 passed |
| `tests/test_loader.py`, `tests/test_fix01.py` | 134 passed; 60 passed, 1 skipped |
| guards: `test_storage_anchored_writes.py`, `test_property_settings.py`, `test_field_contracts.py` | 159 passed, 5 skipped; 1 passed; 502 passed, 34 skipped |
| `tests/test_kb_sync_flow.py` (alone, last) | 28 passed in 75.07s. A first start was stopped by me within a minute, before any result, because the runner's 40-minute cap was below the expected hour; its orphan processes were stopped and it was rerun once |

**New tests**
- `tests/test_stage_flow.py`: for K1 images (with undecodable images, with and without content-chosen HTTP failures),
  K1 texts, K2 naming (seeded rng, unusable assessments, HTTP 500, schema-invalid reply, two spellings of one concept),
  K2 observation, K3 and K4 (HTTP and schema failures): `max_concurrency` 3 equals 1 in rows, report minus
  `capacity`, checkpoint rows and stored artifacts (call-record latency stripped), with peak in flight 3 against 1 and
  the capacity record checked. K1 also: identical batch composition, one outcome per image. Fatal drains: K4 and K1
  (labelled storage failure mid-run: no request after it beyond the items already running, every answer received
  checkpointed). Stops: K4 and K2 naming cancel at once, leave no task, and the resume asks only what was not done
  and equals the serial rows. Override and refusal: K1 decode pool peaks at `runtime.cpu_workers` 3; 0,
  `pin_required` and `True` refused before any read. S1 dense: same candidates and batches, peak 3; a content-chosen
  failure surfaces identically. S2: the real planner with delayed real S1 lookups equals the serial run and an
  independent serial-loop oracle of the added shortlist order; one failed lookup warns `RETRIEVAL_FAILED`.
- `tests/test_model_transport.py`: connect, connect-timeout and pool-timeout errors are retried; read timeout, write
  timeout, read error and remote protocol error are not (7 cases).
- `tests/test_embed_server_read_set.py`: above `server.max_pending_requests` refused and equal or below accepted, for
  all 4 callers; `max_pending_requests` moved from the unread to the read leaves.
- `tests/test_kb_sync_production_probes.py`: one run and at most one registry build per sync.

**Mutants** (16, scratch import-hook plugin, one pytest process each; the scratch is deleted)

| Mutant | Result |
|---|---|
| K1: an undecodable image drops the rest of its chunk | killed (after adding the one-outcome-per-image check) |
| K1: decode pool ignores `runtime.cpu_workers` | killed |
| K1: text flow serial | killed |
| K1: chunk composition depends on the flow limit | killed |
| K2 naming: draws not gated in folder order | killed (after making the first folder's assessment slow) |
| K2 naming: last folder's concept row wins | **survived: equivalent** (`apply_name_overrides` rewrites `display_name` from memberships in folder order, `k2_naming.py:171-178`; the other row fields are equal) |
| K2 naming, K2 observation, K3, K4, S1, S2: flow limit 1 | killed (6) |
| S2: merge in reverse mention order | killed (after adding the serial-loop oracle) |
| transport: retry `ReadTimeout` | killed |
| config: refuse equal admission slots; check removed | killed (2) |

**not_run**
- `ruff check`, `ruff format --check`: ruff is not installed in `.venv`; installs are not allowed.
- Live backends, GPU, wall-clock throughput: out of scope (CPU doubles only).

**Configuration audit (Part B)**

| Key or literal | Class | Override test | Refusal test |
|---|---|---|---|
| `vlm.backends.<role backend>.max_concurrency` | `configurable`, existing | peak 3 with 3, 1 with 1, in K2 naming, K2 observation, K3, K4 (`test_stage_flow`) | 0 and non-integers refused at load (existing) and by `flow` (`test_model_resources`) |
| `vlm.backends.<embedder>.max_concurrency` | `configurable`, existing | peak 3 in K1 images, K1 texts, S1 dense (`test_stage_flow`) | **new**: above `server.max_pending_requests` for `embed_server` (`test_embed_server_read_set`) |
| `vlm.backends.<embedder>.server.max_pending_requests` | `configurable`, existing; now also read by K1 callers | equal and below accepted | sentinel refused for every caller (`test_a_sentinel_in_a_read_server_leaf_still_refuses`) |
| `runtime.cpu_workers` | `configurable`, existing; now also the K1 decode pool | 3 workers give 3 concurrent decodes | 0, `pin_required`, `True` refused before any read |
| `embedding.batch_size` | `configurable`, existing | chunks of 2 identical at cap 1 and 3 | existing |
| `http.retry_statuses`, `max_retries`, back-off | `configurable`, existing, unchanged | existing | existing |
| `RETRYABLE_TRANSPORT_ERRORS` | `protocol-bound`, new | 7-case transport test | n/a |
| `CAPACITY_FIELDS`, `TIMING_FIELDS` (`in_flight_slot_s` renamed) | `protocol-bound` (PM answer 6) | pinned by `test_model_resources` | n/a |
| flow limit `max_concurrency + 1` for K1 images | `safety-bound`: at most one extra prepared chunk; memory <= batch_size * (cap + 1), transport cap unchanged (fix round 1 PM answer) | covered by the K1 peak test | n/a |
| new profile keys | none | | |

**Changed files (Part B)**
- `src/prompt_enhancer/`: `model_resources.py`, `model_transport.py`, `model_client.py`, `config.py`, `assembly.py`,
  `k1_embed.py`, `k2_naming.py`, `k2_observe.py`, `k3_cards.py`, `k4_aliases.py`, `candidates.py`, `planner.py`
- `tests/`: `test_stage_flow.py` (new), `test_model_resources.py`, `test_model_transport.py`, `test_k1_embed.py`,
  `test_k2_observe_edges.py`, `test_k3_edges.py`, `test_k4_aliases.py`, `test_assembly.py`,
  `test_assembly_properties.py`, `assembly_fixtures.py`, `test_embed_server_read_set.py`,
  `test_kb_sync_production_probes.py`
- `docs/stages/K2-observe.md` (the observation flow sentence was the old batch-and-cancel behaviour); this task file

**Noticed, not done**
- RES-2 handoff: the cross-stage rows of the Part A report stand unchanged.
- `docs/rules/VLM_CALL_RULES.md` has no rule on in-flight capacity or retry classes; a rule 14 (per-item flow up to
  `max_concurrency`, back-off only on 429, overload, OOM or connect-phase errors) is the PM's call, not added here.
- A test double passed as `client=` must now expose `.concurrency` (the run's `BackendConcurrency`), like the real
  `ModelClientRun`; three in-repo doubles were updated.
- The equivalent K2 mutant would become observable only if K0 rows ever carried a non-null `kind` into naming.

## AUTO PROGRESS 2026-11-17T22:21:35+07:00 (PreCompact, trigger=auto)
Written by the worker's PreCompact hook (mechanical); the sections above stay authoritative.
- git status (<project>\.tmp\worktrees\RES-1):
  - `## HEAD (no branch)`
  - `M configs/enhancer.example.yaml`
  - `M contracts/README.md`
  - `M contracts/schemas/enhancer_config.v0.schema.json`
  - `M docs/rules/VLM_CALL_RULES.md`
  - `M docs/stages/S3-knowledge.md`
  - `M src/prompt_enhancer/candidates.py`
  - `M src/prompt_enhancer/config.py`
  - `M src/prompt_enhancer/k1_embed.py`
  - `M src/prompt_enhancer/k2_naming.py`
  - `M src/prompt_enhancer/k2_observe.py`
  - `M src/prompt_enhancer/k3_cards.py`
  - `M src/prompt_enhancer/k4_aliases.py`
  - `M src/prompt_enhancer/kb_sync.py`
  - `M src/prompt_enhancer/model_client.py`
  - `... 40 more lines`
- last changed files: `<project>\.tmp\worktrees\RES-1\tests\test_stage_flow.py`; `<project>\.tmp\worktrees\RES-1\tests\test_k1_embed.py`; `<project>\.tmp\worktrees\RES-1\tests\test_k4_aliases.py`; `<project>\.tmp\worktrees\RES-1\tests\test_model_resources.py`; `<project>\.tmp\worktrees\RES-1\src\prompt_enhancer\model_resources.py`; `<project>\.tmp\worktrees\RES-1\src\prompt_enhancer\planner.py`; `<project>\.tmp\worktrees\RES-1\src\prompt_enhancer\candidates.py`; `<project>\.tmp\worktrees\RES-1\src\prompt_enhancer\k2_naming.py`; `<project>\.tmp\worktrees\RES-1\tasks\active\RES-1-resource-use.md`; `<project>\.tmp\worktrees\RES-1\src\prompt_enhancer\k4_aliases.py`
- last assistant text: Now the differential test file, 'tests/test_stage_flow.py'.
## Fix round 1 approval record

PM approved the scoped stop contract: await executor teardown off-loop, cancel queued work, await every owned worker despite repeated cancellation, check stop before reads/decodes/encoding. Already-running native operations are residuals bounded by cpu_workers and the decode-pixel cap. Other synchronous storage/schema/model-image/S1/S2 operations remain residual, RES-2.
PM approved comparison of vectors, membership, logical records/draws, semantic sync state, rows, failures and report order; exclude physical IDs, timestamps, timing and invocation counts. Persisted samples remain authoritative under seed changes. Prefer full-input chunks with full-membership partial replay, and per-folder seeded generators. Decode-ahead cap+1 is safety-bound.

## Fix round 1

### Step 0 - approved by PM; implementation authorized

R-IDs: R-015, R-017, R-018, R-020; decision 149 / S46. Read original Step 0, PM answers, both completion reports, eight Part B decisions, both reviews and unchanged reviewer probe source. No runtime/test edit or test execution. Decision 150 acceptance comes from the dispatch; the local index lacks that row.

Historical draft blocker (resolved by this dispatch): the preceding plan-only worker lacked an edit tool. This worker successfully used apply_patch and copied the draft here before runtime edits. The draft's proposed recovery artifact is superseded by the PM's accepted full-membership replay design.

May assume: accepted fatal-drain/external-cancel semantics, identical K0 input/config/source availability in paired runs, authoritative persisted samples, CPU synthetic doubles only, no new key under scoped cleanup option.
Must ask: Q1 stop-bound/scope; Q2 exact comparison boundary. No Git writes, installs/downloads, GPU/model calls, extra agents or PM-owned shared edits.

Unknowns/failure modes:
- Both K1 paths chunk after done filtering. Stable boundaries alone miss partial per-vector commits. A resumed reduced request changes batch-sensitive vectors. Per-item keys omit companions (k1_embed.py:140-165); durable batch identity and whole-response recovery are needed. Do not force serial/batch-size-one execution or repeat completed paid requests.
- K2 cached draws skip RNG advancement (k2_naming.py:407-434). A fresh identical seed can misassign later samples even without fatal drain. Suppressing later draws alone is insufficient. Preserve changed-seed cached reuse (tests/test_k2_naming.py:63-95) and custom sampling doubles.
- Threads cannot preempt an executing OS read/native decode. Async join preserves loop responsiveness but does not bound total cleanup. Other synchronous storage/model-image/online callees exceed edit scope (Q1).
- Dict equality misses order. Fresh/resumed accounting and physical log history differ by definition (Q2).

### Class sweep A - interruption and ordering

Pre-fix references; Required is proposed, not implemented evidence.

| Path | File:line | Decides it today | Required determinant/check |
|---|---|---|---|
| K1 images | k1_embed.py:315-327,421-468 | Pending IDs after cache filter; partial commits shrink requests | Full sorted-input chunks; durable membership/refusals/whole response; recover partial materialization with identical vectors/context |
| K1 image reports | k1_embed.py:404,438-486 | Table sorted; failure maps follow completion; cached refusals omit report entries | Stable per-ID outcomes, explicit map ordering, separate invocation accounting |
| K1 texts | k1_embed.py:502-547 | Sorted unique texts then cache filter/chunks | Full unique-text chunks and whole-response recovery; batch-sensitive property; sort failures |
| K2 draws/assessment | k2_naming.py:267-272,394-441,578-686 | Ordered gate; only new draws consume RNG; fatal drain persists later draws | Persisted samples plus reproducible logical RNG position; preserve cached samples under changed resumed seed |
| K2 naming rows/report | k2_naming.py:522-525,546-568,662-732 | Folder-ordered effects; failed dict follows completion | Same first-folder spelling, draws/redraws/checkpoints, sorted failures/artifact references |
| K2 observation | k2_observe.py:189,213-227,235-320 | Global representatives; per-key cache; sorted rows/lists, timing-ordered failures | Same full-input grouping, representatives, documents; deterministic failure-map iteration |
| K3 | k3_cards.py:217,252-254,263-355 | Normalized observations; grouping checkpoint before card; timing-ordered failures | Same payload/evidence/reply across both boundaries; sort failures |
| K4 | k4_aliases.py:158-216,217-262 | Per-concept generation cache; ordered alias merge; timing-ordered failures | Same reply/provenance across generation/document boundaries; sort failures |
| S1 | candidates.py:182-205 | Complete query chunks and input-order merge; no durable resume | Same queries/config/pin; restart rebuilds full batches; retain differential/failure tests |
| S2 | planner.py:306-377 | Dependent replies; sorted mentions and input-order lookup merge | Same supplied replies/pin and ordered warnings/results; cancel leaves no lookup task |
| flow/gate | model_resources.py:195-254 | Timing affects admission; earliest item-index error after drain | Same authoritative outcomes/RNG position despite extra attempted work; ordered results/errors, no admission after error, full child cleanup |

### Class sweep B - blocking calls from async paths

A resource/byte bound is not a time bound. Q1 rows below are OPEN acceptance hazards, not claimed fixes. Moving a wait off-loop does not bound its total duration.

| Reachable call | Bound before | Required after / proof obligation |
|---|---|---|
| Executor shutdown, k1_embed.py:469-472 | Unbounded synchronous join of at most cpu_workers running calls | No join on loop; cancel queued work, cooperative stops, async await all owned workers despite repeated cancel; total deadline for stuck work needs Q1 |
| K1 read/hash/decode, k1_embed.py:365-389,412-434; k0_scan.py:189-218 | Off-loop; batch_size*(cap+1) admitted inputs, cpu_workers running; OS/native call unbounded in time | Keep resource caps/read-once preparation; stop checks before read/decode/encode; preempting stuck call needs killable isolation (Q1) |
| K1 checkpoint lookup/load/commit/fail, writer entry/exit, k1_embed.py:337-362,418-468,507-535; storage/store.py:743-766,1125-1225 | Sync hash/read/write/RLock; OS lock retry limited by storage.lock.timeout_s plus poll, syscall time unbounded | Broad guarantee requires serialized async storage boundary, writer entry/exit on one owner, no orphan operation; external-writer ownership and total deadline need Q1 |
| K2 naming info/overrides/recovery/image reads, k2_naming.py:317-366,486-507,640-658 | Sync reads/hash/parse; input/sample-count bounds, no I/O deadline | Owned off-loop operations if broad guarantee applies; RNG/state remain ordered; await cleanup of reads (Q1) |
| K2 observation read/validation, k2_observe.py:267-291 | Sync verified read/schema/catalog work; no time bound | Same source-operation ownership/cleanup; Q1 for arbitrary blocked I/O/dependency scope |
| K2-K4 checkpoint writes/reads and writer cleanup, k2_naming.py:275-290,725-730; k2_observe.py:238-300,308-313; k3_cards.py:273-320,340-345; k4_aliases.py:169-201,243-260 | Sync storage/locks as K1 | Same async ownership boundary if required; retain artifact-first/state-last and fatal drain; Q1 |
| Schema/revision reads and cache locks, config.py:382-413; model_schemas.py:41-57,66-68; k1_embed.py:92-95; k2_observe.py:280-282 | Full read/hash, brief LRU lock; no I/O/contention deadline | Measure hit/miss cost; exact-byte identity, no stat-only shortcut; moving all callers off-loop exceeds allowed scope (Q1) |
| Model-image preparation, model_client.py:670-693; model_image.py:19 | Sync decode/resize/encode/base64 and backstop lock; pixels/samples bounded, not time | Preserve role settings; cancellable preparation needs outside-scope model_client change (Q1) |
| S1 search/S2 schema/pin reads, candidates.py:192-205; planner.py:292,297,367 | Sync search/read dependent on data size; no elapsed bound | Broad guarantee requires caller/storage edits outside scope (Q1); retain cancellation/ordering checks |
| Async flow/gate/HTTP/close, model_resources.py:195-254; model_transport.py:41-48,60-84; model_client.py:217-222 | Waits yield; configured HTTP timeouts/retries; cleanup depends on callees | Keep yielding waits, cancel/gather ownership and awaited close; repeated-stop/fatal-drain tests; HTTP timeout does not bound sync I/O |

### Questions for PM (ranked, proposed answers)

1. Stop bound/scope: no synchronous teardown wait on loop with cooperative/off-loop cleanup, or finite total stop time even for stuck reads/native decoders? Proposed: scoped former option with all owned work awaited and explicit residual limitations. If latter is mandatory, expand scope before code for killable preparation, validated deadline and synchronous storage/model-image/online callers. Thread timeout or shutdown(wait=False) cannot meet it. Literal broad requirement plus current scope leaves open rows and prevents PASS.
2. Exact comparison: proposed byte-identical vectors, stable batch membership/identity, logical records/draws/redraws, latest semantic sync state, rows, failure set and deterministic report order for identical input/config/restarted seed. Exclude physical run IDs/timestamps/timing; validate per-invocation embedded/reused/skipped/request counts separately. Resume skips completed requests. Persisted samples remain authoritative under deliberate seed change. Doubles supply identical replies; no live model reproducibility claim.

### Configuration impact - before runtime edit

No new profile/schema/key under scoped Q1 option; hard-deadline option needs revised table first.

| Operational value | Effect / evidence to run | Class |
|---|---|---|
| embedding.batch_size (existing config validation/profiles) | Stable chunks; sizes 1/2/3 with cached/refused/changed members and every boundary; loader/stage refusal | configurable |
| vlm.backends.<b>.max_concurrency (backend validator/profiles) | Preserve sliding admission/transport cap; caps 1/2/3, peak assertions and invalid-limit refusal | configurable |
| runtime.cpu_workers (k0.resolve_workers/profiles) | Same worker cap, owned async teardown; workers 1/3 peak/heartbeat; 0/True/sentinel refusal | configurable |
| knowledge.naming.sample_size/selection/rename_on_change | Same sampling/redraw policy; vary populations/sample/partial cache; existing naming/fix-round override/refusal checks | configurable |
| sync.resume, storage.lock timeout/poll, HTTP timeout/retries | No intended setting change; existing checks plus changed-boundary tests | configurable |
| cap+1 decode-ahead chunk | Supersedes invalid Part B design-bound: fixed admission/memory safety policy, prepared images <= batch_size*(cap+1); transport cap unchanged; peak/cleanup tests | safety-bound |
| Durable batch context/whole response if needed | Versioned recovery artifact; all commit boundaries, malformed/incompatible context refusal | artifact-bound |
| Report/capacity fields | Sort failures; assert list(mapping.items()) under reversed completion | protocol-bound |
| Fault points/batch-sensitive/seeded doubles | Test only; show actual red before fixes | test-only |

### Research evidence - Parts A, B and this round

Raw primary sources actually read this turn; package version metadata still to confirm, not inferred from reviews.

| Question / part | Source read | Finding | Class |
|---|---|---|---|
| Executor shutdown B/fix | Installed C:/Users/ADMIN/AppData/Local/Programs/Python/Python312/Lib/concurrent/futures/thread.py:54-65,220-239; _base.py:364-381 | Queued work cancels; running future refuses cancel; wait=True joins threads without timeout | source-verified |
| Async cancellation A/B/fix | Same stdlib asyncio/futures.py:367-421; tasks.py:210-226,791-820,873-928; threads.py:12-25 | Wrapper cancellation attempts source cancel; task cancellation at next loop cycle; gather awaits; shield needs owned reference; to_thread uses executor | source-verified |
| HTTP exceptions B | Main .venv/Lib/site-packages/httpx/_exceptions.py:123-190; local model_transport.py:9-12,53-85 | Distinct connect/read/write/pool exceptions; selected connect-phase classes/statuses retried | source-verified |
| Snapshot cache A | model_schemas.py:14-58,66-117; config.py:382-413 | Same bytes digest/build; publish after matching reread; lock only LRU, registry get copies | source-verified |
| Storage A/B/fix | storage/store.py:725-810,1112-1225; storage/_fs.py:182-201 | RLock; artifact-first/state-last; fresh done verification; configured OS lock retry deadline is not syscall deadline | source-verified |
| Resume batches/draws | k1_embed.py:315-327,421-470,510-535; k2_naming.py:394-441,578-686; tests/test_k2_naming.py:63-95 | Pending-only chunks and RNG-cache skip; changed-seed cached reuse required | source-verified |
| Reviewer repros | RES-1-rev/.tmp/RES-1-rev/codex/test_review_probes.py:15-79; review-codex.md:19-34,79 | Batch-sensitive double and real threaded-stop test read unchanged; two failures reported, not yet rerun here | source-verified design; reported experiment pending rerun |

Before relevant code: verify installed package versions and hashlib/threading/jsonschema/referencing implementation details as needed. Measure unchanged _schema hits/misses for actual large/small schema sizes through test_slot.py; retain full reads with figure unless alternative preserves exact-byte cache/race properties. Prior ~0.7 ms is a lead, not my measurement.

Read-only evidence: main .venv/Scripts/python.exe exists; worktree-local .venv absent. recall.py --help read; build returned indexed=3 unchanged=91 removed=0 refused=0 chunks=138 skipped_chunks=0. Search RES-1 resume interruption seeded shutdown returned no hits. Direct index/S46 reads confirmed decision 149. STATUS/lessons digest read. rg and Read/Grep unavailable, scoped PowerShell fallback used. One read-format command rejected and corrected. apply_patch denied exact role allowlist; oversized scratch shell write corrected into bounded chunks. No runtime/test source mutation or tests/probes/services started.

### Plan and finish checks

1. Current - Step 0 gate: resolve Q1/Q2, repair edit-tool binding, transfer this section to task file and obtain explicit approval. Complete installed-source/version checks before corresponding code edit.
2. Red first: promote both reviewer tests unchanged including helpers; interrupted-run properties for K1 images AND texts and seeded K2 naming. Enumerate every checkpoint boundary within generated cases; partial successful batches, decode refusals, changed sources, fatal drain/repeated stops; vary cap/workers/batch/sample. Record red before runtime changes.
3. Implement: stable complete-input batch context, durable whole-response recovery and partial materialization; seeded logical progression preserving cached samples; Q1-approved owned async cleanup with no abandoned worker; canonical failure maps K1-K4. Document client/concurrency contract at in-scope stage entry points/double constructors. Preserve capacity/accuracy.
4. Verify: one pytest file/process at a time via test_slot.py, no -n; main project venv, PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=worktree/src, -X faulthandler. Fixed .tmp/RES-1/bt removed after each. Files: test_stage_flow, test_k1_embed, test_k2_naming, test_k2_observe_edges, test_k3_edges, test_k4_aliases, test_model_resources, test_model_schemas, test_candidates, test_planner, every test_k2_fix_round*.py, test_kb_sync_production_probes, test_field_contracts, test_storage_anchored_writes, test_property_settings and new repro/property files. Keep cache-race/same-mtime tests. Run 4-8 scratch-only mutants: pending-only chunks, partial-response recovery, cached RNG progression, synchronous shutdown, unsorted failures, cleanup ownership.
5. Handoff: limiter-run _schema measurement; attempt scoped ruff check/format with exact output; row-by-row config audit, Completion report/Handoff; retain small evidence, remove owned scratch. Full suite/test_kb_sync_flow.py reserved to PM. Stop/report same-check failure twice.

Finish only when unchanged probes pass, new properties shown red then green, all listed files pass, 4-8 mutants killed/explained, research/config evidence complete, no unresolved required-bound/interruption-dependent result, all owned work stopped. Tests/mutants/measurement/lint not_run at Step 0.

Historical draft handoff: plan only. Superseded by the PM approval record above and the implementation progress below.

### Fix round 1 steps

1. Done: copied the approved Step 0 and PM answers before runtime edits; re-read installed executor, asyncio shield/future/thread and httpx exception sources, repository cache and storage implementations. Recall build: `indexed=0 unchanged=94 removed=0 refused=0 chunks=0 skipped_chunks=0`; search `resume seeded shutdown`: no hits.
   Done: reproduced both reviewer failures unchanged (only redundant pytest_plugins registration removed for suite collection): `2 failed in 3.02s`; resume batches `[2, 2]` versus `[1, 2, 1]`, event-loop release delayed `1.0` seconds. New image/text/seeded properties were red, with real vector/sample differences (logs under `.tmp/RES-1/red*.log`).
2. Done: complete-input K1 chunks, cached refusal positions, full partial-batch replay with missing-only writes; seeded K2 per-population generators; sorted report maps; off-loop joined executor and repeated-cancel ownership. No recovery artifact: replaying the identical full request suffices within the approved same-input/configuration boundary.
   Done: reviewer tests `2 passed in 1.64s`; stable checkpoint properties plus real-thread repeated stop `4 passed in 96.98s`. An earlier property run overlapped source edits and invalidated source-hashed checkpoint identity; it is stale, not passing evidence. Runtime source is now frozen during checks.
3. In progress: run all listed files one per process, six scratch mutants, schema measurement and scoped lint attempts; retain actual commands/PIDs/exits in `.tmp/RES-1/results.jsonl`.
   Remaining scope gap: `model_image.py:125-188` combines decode/resize/encode inside one call. K1 checks before read and decode but cannot check before the internal encode without an optional callback in this out-of-scope file. No unauthorized edit made. The question tool was rejected by the role tool allowlist; report this required check as unfinished unless PM authorizes that small scope extension.

### Class sweep A - implemented determinants (source-verified; supplements the pre-edit table)

| Path / references | Before | After / interruption invariant |
|---|---|---|
| K1 images, k1_embed.py:316-324,424-485 | Pending-only chunks; refused checkpoints removed positions | Full image-ID order and batch_size; cached refusals retain positions; wholly done batches skip; partial batches replay full decodable membership, write only missing records. Source-changed companions are reverified but a request with no missing vector skips. |
| K1 texts, k1_embed.py:536-566 | Pending-only chunks | Full unique sorted text chunks; same full-membership replay and missing-only writes. |
| K1 records/report, k1_embed.py:431-498 | Cached decode failures omitted; completion-ordered maps | Cached decode_failed restored; failures/decode_failed sorted; logical records equal at every checkpoint boundary. Invocation counts remain invocation-specific. |
| K2 naming, k2_naming.py:394-444,586-736 | Shared RNG stream skipped cached draws | Random(seed) state + folder/population determines independent local generator; persisted samples win; custom sampling doubles unchanged. Draw/head updates and row/report effects remain folder-ordered. |
| K2 observation, k2_observe.py:189-227,307-324 | Global deterministic representatives; per-item cache; unordered failed map | Same full-input grouping/selection/checkpoints, sorted report failed/queued/references and observations. |
| K3, k3_cards.py:252-347 | Per-concept grouping reply then document; unordered failed map | Same normalized evidence and reply checkpoint at either boundary; sorted cards/concepts/lint and failed map. |
| K4, k4_aliases.py:158-262 | Per-concept generation then ordered merge/document; unordered failed map | Same raw reply/provenance/cache and sorted alias merge; failed map sorted. |
| S1, candidates.py:182-205 | Complete query chunks, input-order merge; no durable checkpoint | Unchanged: inputs/configuration/pinned snapshot determine batches/search order; restart rebuilds same request groups. |
| S2, planner.py:306-377 | Reply-dependent planner sequence, sorted mentions, input-order lookup merge | Unchanged: identical replies and pin determine same sequence and warnings; flow cleanup now owns children despite repeated stop. |

### Class sweep B - scoped final bounds (source-verified)

Q1 permits byte/resource bounds for existing synchronous residuals; these are not elapsed-time guarantees. Normal/error/stop teardown now performs no synchronous executor join on the event loop.

| Reachable operation / references | Before | After / bound |
|---|---|---|
| K1 executor teardown, k1_embed.py:487-496 | Synchronous unbounded thread join on loop | `to_thread(shutdown(wait=True,cancel_futures=True))`, owned through shield and awaited despite repeated cancellation. Queue <= batch_size*(cap+1); at most cpu_workers native operations; no synchronous join. Already-running read/native preparation residual has no elapsed deadline (PM accepted). |
| K1 read/prepare, k1_embed.py:366-377,416-421,445-446 | Off-loop bounded admission, no cooperative stop | Stop before each source read and before decode; queued futures cancelled; all pool workers joined. At most cpu_workers active and batch_size*(cap+1) prepared images, each capped by max_decode_pixels. Pre-encode stop check remains the explicit scope gap above. |
| K1-K4 checkpoint read/hash/write and writer entry/exit, storage/store.py:725-810,1112-1225 | Synchronous RLock, artifact bytes/data-size bound; lock timeout/poll | Unchanged; bounded by stored artifact/table size and configured storage lock timeout/poll (OS syscall time not preemptible). Residual, RES-2. Artifact-first/state-last retained. |
| Naming info/override/recovery/source reads, k2_naming.py:317-444,486-507,640-658; observation read, k2_observe.py:268-291 | Synchronous input-sized reads/parses; selected images bounded by sample_size/representative settings | Unchanged same size/configuration bounds; residual, RES-2. |
| Schema/revision reads and cache locks, config.py:382-413, model_schemas.py:14-117 | Read/hash of finite schema/module bytes; brief LRU lock | Retained exact-byte cache; file-count/byte-sized work, cache capacities 128/16; residual, RES-2. Measurement recorded below. |
| Model-image preparation, model_client.py:670-693, model_image.py:125-188 | Synchronous pixel/sample-bounded preparation and backstop lock | Unchanged; role image max_decode_pixels/max_side/sample count; residual, RES-2. K1's off-loop pre-encode check needs scope extension. |
| S1 search/S2 pin/schema reads, candidates.py:192-205, planner.py:292-297,367 | Synchronous pinned-data-size search/read | Unchanged finite pinned data/depth/configuration bound; residual, RES-2. |
| flow/HTTP/async close, model_resources.py:212-269, model_transport.py:41-85, model_client.py:217-222 | Yielding waits; configured timeout/retries; repeated cancel could abandon cleanup | Cancel children once, await shielded cleanup and owned closes despite repeated cancel. HTTP timeout/retry settings unchanged; cleanup duration depends on owned callee, never a synchronous thread join on loop. |

### Research evidence - re-read for this implementation (Parts A, B and fix round 1)

Installed versions: Python 3.12.10, httpx 0.28.1, jsonschema 4.26.0, referencing 0.37.0, Pillow 11.3.0, Hypothesis 6.170.15. Stdlib root: `C:/Users/ADMIN/AppData/Local/Programs/Python/Python312/Lib`; packages: project `.venv/Lib/site-packages`. These are local source observations, not live model or network evidence.

| Question / part | Source actually read | Raw or summary | Finding | Class |
|---|---|---|---|---|
| Executor ownership, B/fix | stdlib `concurrent/futures/thread.py:220-239`, `_base.py:364-381` | Raw installed source | shutdown cancels queued futures and wait=True joins threads; a running future refuses cancel. Async join must await all owned workers, not abandon them. | source-verified |
| Cancellation, A/B/fix | stdlib `asyncio/tasks.py:705-777,866-928`, `futures.py:367-421`, `threads.py:12-25` | Raw installed source | gather propagates first exception without cancelling siblings; wrapper cancellation cannot stop running native work; shield prevents child cancellation but parent still receives CancelledError. await_owned retains/awaits its child across repeated cancellation; _embed now uses flow to drain/own every request body. | source-verified |
| HTTP error selection and close, B/fix | installed `httpx/_exceptions.py:123-204`, `_client.py:1978-1988`; local model_transport.py:9-12,41-85 | Raw installed source | Distinct connect/read/write/pool exceptions; only configured statuses/connect-phase classes retry. AsyncClient close awaits transport/proxy closes. No new timeout/retry/default. | source-verified |
| Schema checking and resources, A | installed `jsonschema/validators.py:306-318`, `referencing/_core.py:195-215,520-538` (exact excerpts in `.tmp/RES-1/schema-measurement.log`) | Raw installed source | check_schema validates against the selected metaschema; Resource detects specification; registry adds resources without crawling. Cached local schemas retain these checks on misses. | source-verified |
| Snapshot/cache correctness, A | local model_schemas.py:14-117; config.py:382-413; tests/test_model_schemas.py | Raw source | Hash/build share exact bytes; matching reread gates publication; failures never publish; private schema copies; same-size/mtime and concurrent writer tests retained. Config file still fully read each call. | source-verified |
| Cache cost, A/Claude P3 6 | limiter-run `.tmp/RES-1/measure_schema.py`, output `.tmp/RES-1/schema-measurement.log` | Actual bounded CPU run, 3 cold builds and 100 hits per schema; other focused tests active | request.v0: 592 bytes, mean miss 2.523 ms, hit 0.570 ms (median 0.559). enhancer_config.v0: 204538 bytes, mean miss 185.191 ms, hit 0.788 ms (median 0.755). Keep full reads: small hit cost preserves byte-exact race guarantees. No speed-up claim. | experiment |
| Checkpoint ordering, B/fix | storage/store.py:1112-1225; k1_embed.py:424-570; k2_naming.py:394-444 | Raw source | Artifact first, state last. Stable full-input chunks survive partial item commits by replaying full membership; cached members are not rewritten. Per-population seeded generators remove skipped-stream position; persisted draws/custom sampling doubles remain authoritative. | source-verified |
| Seeded generator construction, fix | installed stdlib random.py:125-178 | Raw installed source | getstate returns version/internal state/gauss cache without a draw; Random(string) uses all string bits under seed version 2. Each population receives an explicit digest seed; no ambient stream advancement. Production SystemRandom and custom sampling objects remain unchanged. | source-verified |
| Regressions and properties, fix | tests/test_res1_review_probes.py; test_res1_resume.py; `.tmp/RES-1/red*.log`, stable-test_res1_resume.log | Actual CPU labelled doubles | Original two test bodies unchanged; both red then green. Image/text vectors and seeded samples showed real pre-fix differences. Stable property run covers every durable checkpoint boundary in generated cases, including fatal drain and a repeated native-worker stop. | experiment |

Measurement command (exit 0; environment same as tests): `<python> -B tools/agent-workflow/test_slot.py -- <python> -B -X faulthandler .tmp/RES-1/measure_schema.py > .tmp/RES-1/schema-measurement.log`. The script is scratch and removed at handoff; its small source excerpts/results remain in the log.

### Configuration audit - fix round 1

No profile/schema/example/key is added. Existing schema source is `contracts/schemas/enhancer_config.v0.schema.json`; examples remain `configs/enhancer.example.yaml` and `configs/enhancer.dev.example.yaml`. No provider/model/precision/image policy/spend change. No hidden deadline or pool-size default.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override and refusal evidence | Retained-literal class |
|---|---|---|---|---|---|
| Complete-input embedding chunks | embedding.batch_size | validate_embedder_reads + schema, _validate_embedding cross-field checks | Existing examples; no change | test_k1_embed::test_batch_size_override; test_res1_resume::test_k1_every_checkpoint_boundary varies 1-3; test_field_contracts::test_config_override_and_refusal[embedding.batch_size] | configurable |
| Request/admission limits | vlm.backends.<b>.max_concurrency | Existing positive integer/backend read validation; flow rejects non-positive/non-integers | Existing examples; no change | test_stage_flow serial/cap3; test_model_resources::test_in_flight_follows_configured_limit, test_flow_limit_refused, test_zero_concurrency_refused; field-contract override/refusal | configurable |
| Decode workers / async shutdown | runtime.cpu_workers | _cpu_workers -> resolve_workers, before reads/calls | Existing auto profile value; no change | test_stage_flow::test_k1_decode_pool_follows_cpu_workers (3), test_k1_refuses_an_unusable_cpu_worker_count_before_any_read (0, True, sentinel); test_res1_resume repeated stop (1); generated workers 1/3 | configurable |
| Sampling / redraw / persisted reuse | knowledge.naming.sample_size, selection, rename_on_change | validate_naming_reads; existing closed schema/domain checks | Existing sample_size=5; no change | test_res1_resume seeded property varies sample 1-3; test_k2_fix_round1::test_changed_sample_size_gets_new_draw; test_k2_fix_round3::test_invalid_sample_is_keyed_configuration_refusal_before_binding; test_k2_naming::test_resume_reuses_committed_draw_and_completed_calls preserves changed-seed reuse | configurable |
| Resume choice, role/image/provider/HTTP settings | sync.resume; existing model/vision/HTTP configuration | Existing unchanged read validation | All existing profiles unchanged | test_k1_embed resume_false referenced/unreferenced tests; field-contract override/refusal; existing stage and production probe tests | configurable (unchanged) |
| One chunk of decode ahead | Derived max_concurrency + 1, no new key | Transport still caps requests; prepared images <= batch_size*(cap+1) | No deployment choice added | test_stage_flow capacity/worker limits, test_res1_resume repeated-stop worker join | safety-bound |
| Stop/cleanup state booleans and Event | No profile field; task-lifecycle state only | No material numeric default; no elapsed deadline claimed | Not applicable | model_resources repeated-stop tests and real threaded K1 stop test | safety-bound |
| Full-membership partial replay / record layout | Existing LAYOUT_VERSION=1 and checkpoint keys; no new artifact | Same _load_record checks and storage integrity checks | Not applicable | interrupted image/text properties: exact vectors/records/latest state and batches; fully checkpointed requests never repeated | artifact-bound |
| Seeded per-population hash field names | rng_state, population; injected Random state plus existing folder/member identity | No deployment setting/new seed; production SystemRandom and explicit sampling doubles retain their contract | Not applicable | seeded differential property; existing FixedDraw and changed-seed resume tests | artifact-bound |
| Ordered report map keys and capacity fields | Existing report protocol | sorted failure/decode_failed maps; telemetry unchanged | Not applicable | test_stage_flow::test_failure_maps_sorted_under_reversed_completion, all six K1-K4 paths; existing capacity field tests | protocol-bound |
| Internal request-body tasks | len(bodies), derived from configured request shape/batch_size | Transport cap retained; flow owns/drains tasks in order | Not applicable | test_k1_embed vllm/chat and HTTP failure cases; flow error/stop properties | protocol-bound |
| Cache read policy / capacities | No new setting; SCHEMA_CACHE_CAPACITY=128, REGISTRY_CACHE_CAPACITY=16 unchanged | Byte hash/build/reread, bounded LRU | Not applicable | full test_model_schemas cache/writer tests; measurement above | safety-bound |
| Generated seeds, waits, fault points, batch-sensitive vectors | Tests only | Labelled doubles; no live inference or time budget setting | Not applicable | New property/reviewer/reversed-completion/repeated-stop tests | test-only |
| Pre-encode cooperative stop | Approved model_image.py scope extension; no profile field | DecodeStopped plus worker-local callback at open/EXIF/load/encode; no result is checkpointed | Not applicable | Resolved by extension tests test_res1_decode_stop below; native operations already running retain the accepted resource bound | safety-bound |

### Completion report - fix round 1 before scope extension (2026-11-18; historical)

**Step 0:** approved draft and explicit PM answers are recorded above. No second stop was required by this dispatch. R-IDs: R-015, R-017, R-018, R-020; decision 149 / S46.

**Work completed / behavior:**
- `k1_embed.py`: image and text batches derive from all sorted inputs before cache filtering; cached decode refusals retain their positions and are restored to the semantic report. Fully checkpointed requests skip. Partial item commits replay full original membership and write only missing records. Revalidating a source-changed companion never repeats an otherwise fully checkpointed request. No new recovery artifact or batch-size-one workaround.
- `k2_naming.py`: a plain seeded Random gets a folder/population-derived local generator, so cached/drained folders cannot shift later samples. Cached samples, redraw policy and custom sampling doubles remain authoritative, including a changed resume seed.
- `model_resources.py`: await_owned shields cleanup, retains its task, awaits it across repeated cancellations, then propagates cancellation. flow cancels/awaits every running child on stop. K1 owns/drains every body request through flow, too.
- K1 thread teardown runs off-loop, cancels queued futures, checks stop before reads/decode and joins every owned worker before returning. Already-running OS/native preparation remains the PM-accepted resource-bounded residual. Pre-encode checking inside model_image is the one unfinished scoped requirement below.
- K1-K4 `failed` maps, plus K1 `decode_failed`, sort keys explicitly. Stage docstrings and the actual doubles in test_k2_observe_edges/test_k3_edges/test_k4_aliases explain the run-owned concurrency contract. A custom injected object is treated as a run (`.concurrency` + async `for_role`); only an actual BoundModelClient (including subclasses) is unwrapped via `.run`.
- `_schema` is unchanged: full-byte read/hash remains, with the measured hit costs and source evidence above. The old Part B `design-bound` row is corrected to `safety-bound` with its explicit prepared-memory bound.

**Result and evidence** (unit/mock_contract/property, CPU synthetic doubles only): exact absolute commands, PIDs, exit codes and actual pytest summaries are retained in `.tmp/RES-1/results.jsonl`. One file per pytest process; no `-n`; no simultaneous owned pytest processes. Every test passed through test_slot.py; fixed `.tmp/RES-1/bt` is deleted after each completed run.

Command form, from this worktree: `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q tests/<one-file>.py --basetemp=<project>/.tmp/worktrees/RES-1/.tmp/RES-1/bt`. Environment: PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=this worktree's src. CREATE_NO_WINDOW for owned child launchers.

| File in tests/ | Actual passing summary |
|---|---|
| test_res1_review_probes.py | 2 passed in 1.64s |
| test_res1_resume.py | 4 passed in 96.98s (0:01:36) |
| test_stage_flow.py | 24 passed in 33.54s |
| test_k1_embed.py | 68 passed, 1 skipped in 23.48s |
| test_k2_naming.py | 24 passed in 12.20s |
| test_k2_observe_edges.py | 20 passed in 7.64s |
| test_k3_edges.py | 11 passed in 10.23s |
| test_k4_aliases.py | 44 passed in 8.72s |
| test_model_resources.py | 23 passed in 1.31s |
| test_model_schemas.py | 14 passed in 1.82s |
| test_candidates.py | 34 passed in 30.98s |
| test_planner.py | 67 passed in 12.34s |
| test_k2_fix_round1.py | 39 passed in 15.58s |
| test_k2_fix_round2.py | 42 passed in 28.34s |
| test_k2_fix_round3.py | 35 passed in 7.32s |
| test_kb_sync_production_probes.py | 1 passed in 4.64s |
| test_field_contracts.py | 502 passed, 34 skipped in 221.24s (0:03:41) |
| test_storage_anchored_writes.py | 159 passed, 5 skipped in 10.78s |
| test_property_settings.py | 1 passed in 4.81s |

Total: **1114 passed, 40 skipped, 19 files**. Skips are reported as skips, not passed checks. All existing test_k2_fix_round*.py files were discovered and run (rounds 1-3).

**Red first and verification scope:** reviewer test bodies were copied unchanged (the external-only duplicate pytest_plugins declaration is omitted to avoid registering conftest twice). Both failed before fixes (`2 failed in 3.02s`), including a measured 1.0-second loop stall, then passed. The new K1 image/text and seeded K2 properties showed pre-fix mismatches. The initial seeded property also exposed reused Hypothesis fixture directories; unique case directories fix that harness error. A stale run overlapped runtime source edits and changed tool_revision mid-comparison; it is explicitly excluded from passing evidence. The stable run froze runtime source and passed all cases. The old K1 resume count assertion was updated to subtract cached decode refusals while retaining exact invocation counts and adding clean/resumed semantic failure-map equality. The new K3 order-test fixture now supplies a real snapshot ID; no runtime behavior was weakened to satisfy it.

**Important changes and rejected alternatives:** full-membership partial replay is the smaller PM-approved mechanism; a whole-response recovery artifact is unnecessary within the same-input/configuration comparison boundary. No completed member is overwritten, and no fully checkpointed request repeats. Re-keying remains accepted: source/tool identity changes once at integration. The seeded generator removes a shared stream dependency instead of suppressing paid in-flight work. Same-size/same-mtime correctness rules out a stat-only cache shortcut; measured full reads were retained. No deployment knob, timeout or deadline is invented.

**Problems, risks and remaining work:** the required stop check immediately before encoding is inside the out-of-scope shared function `model_image.py:125-188`. All in-scope checks and asynchronous teardown are implemented, but no claim is made that this missing internal check is complete. Proposed minimal extension: an optional cooperative callback at the shared image-preparation read/decode/encode boundaries, wired from a K1 worker-local stop context while preserving `_decode(data, settings)` so the reviewer test remains unchanged. No new configuration value or process isolation. PM must authorize that file extension, or explicitly accept it as a residual. The question tool itself was rejected by the role's exact tool allowlist; no unauthorized file edit or escalation occurred.

**not_run:** full suite and tests/test_kb_sync_flow.py (reserved to PM); live model/GPU/network/download/install work (prohibited); the out-of-scope pre-encode check. Ruff commands were attempted, both exit 1; neither check ran because ruff is absent. Actual output for both `-m ruff check <scoped files>` and `-m ruff format --check <scoped files>`:

```text
<project>\.venv\Scripts\python.exe: No module named ruff
```

The scoped source/test/task `git diff --check` passed with no output. No Git write was performed. Source parser/validator properties, concurrent cache writers and the field/storage/property guards passed above; no quality or live throughput claim follows from doubles.

**Changed files in this round:** src/prompt_enhancer/{k1_embed,k2_naming,k2_observe,k3_cards,k4_aliases,model_resources}.py; tests/{test_k1_embed,test_k2_observe_edges,test_k3_edges,test_model_resources,test_stage_flow}.py; new tests/test_res1_review_probes.py and tests/test_res1_resume.py; this task file. No change to config.py, schemas, profiles or examples in this round. The worktree also contains the pre-existing PM catch-up/RES-1 Parts A/B patch; never stage/reset it.

**Lessons for PM:** freeze source throughout a source-hashed differential run; source edits during a property invalidate its oracle. Hypothesis function-scoped storage fixtures need a unique case directory for each generated/shrunk invocation. A threaded stage requires a real thread-stop test; coroutine-only cleanup checks are insufficient.

### Mutants - fix round 1

Six scratch-only module copies, loaded by a scratch import hook; each pytest process used test_slot.py and the same cleaned basetemp. Runtime paths kept their original __file__ solely so tool_revision reads the real dependency tree. No live source or index mutation. Exact commands/PIDs/exits: `.tmp/RES-1/mutants.json`; actual assertion outputs: `.tmp/RES-1/mutant-*.log`. Static verification checked that each exit 1 contains an AssertionError and a failed test, not a collection error.

| Mutant | Result / real pytest output |
|---|---|
| K1 chunk pending images after cache filtering | killed; 1 failed, 1 deselected in 1.31s |
| K1 partial text batch sends missing members only | killed; 1 failed, 3 deselected in 22.15s |
| K2 use shared seeded sampler instead of population generator | killed; 1 failed, 3 deselected in 340.13s (0:05:40) |
| K1 executor shutdown joins on event loop | killed; 1 failed, 1 deselected in 2.17s |
| K2 observation failed map retains completion insertion order | killed; 1 failed, 23 deselected in 2.81s |
| Owned cleanup awaits child without shield | killed; 1 failed, 22 deselected in 0.36s |

The seeded mutant's 340 seconds included Hypothesis shrinking; it finished naturally with a real sample mismatch before its 600-second bound. No timeout/crash was counted as a kill, no forced termination or rerun was needed, and all six limiter processes were awaited. A CIM ownership query attempted while diagnosing its duration was denied by the OS; no escalation or process termination followed.

### Handoff - fix round 1 before scope extension (historical)

- **Historical status: BLOCKED before the pre-encode scope extension was approved.** Superseded by the approved continuation and latest Completion report/Handoff below. In-scope verification at that point is preserved as historical evidence.
- **Already done:** approved Step 0 and both class sweeps; C1/C2 runtime corrections; C3 source/version/experiment evidence; C4 permitted safety-bound classification; all six Claude P3s fixed or explained (the native already-running operation is the accepted residual). Both unchanged reviewer function bodies are verified by AST hash in `.tmp/RES-1/final-verification.txt`; 19 files total 1114 passed/40 skipped; 6/6 non-equivalent mutants killed; schema full-read measurement retained; scoped diff whitespace check passed.
- **Next PM action:** authorize the model_image.py scope extension (or explicitly accept the missing pre-encode check as a residual). Concrete, **unapplied and untested** proposal: `.tmp/RES-1/pre-encode-scope-proposal.patch`. It adds an optional shared-preparation stop callback at open/load/save, uses a worker-local cancellation token in K1 and preserves `_decode(data, settings)` plus existing caller defaults. Apply only after approval, add a held-between-decode-and-save stop regression, and rerun affected image/stage properties. Then obtain the requested fresh independent reviews. PM alone runs test_kb_sync_flow.py/full suite and Git integration.
- **Do not repeat:** no GPU/model/download/install/network work; no whole-tree copying; no stat-only schema cache; no pending-only batching; no shared seeded stream; no synchronous join or abandoned worker; no runtime source edits during source-hashed differential tests. Keep physical run IDs/timestamps/timing/invocation counts outside semantic equality; counts are independently asserted.
- **Configuration audit:** the row-by-row table above is complete for implemented values and explicitly marks the missing pre-encode callback as a defect; no new key/schema/profile. Never interpret the green test counts as closing that known scope gap.
- **Worktree/base/partial patch:** this RES-1 worktree, detached base index ed69118 with the PM catch-up through dcf2320 and unstaged Parts A/B; no stage, reset, commit, push or history command. Tracked edits are the files listed in the Completion report. The proposal patch is separate from the working change.
- **Cleanup/lifecycle:** all owned test/mutant/measurement/static-verification commands returned and were awaited; no service or server started; real-thread stop asserts pool threads exited. Basetemp and mutant source are absent. Iteration scripts/import hooks are removed before final handoff; retain only the small Step 0 report, command/result/pytest logs, measurement excerpts, mutation evidence, final-verification report and unapplied proposal patch under `.tmp/RES-1/`.
- **Tool constraints:** rg and Read/Grep tools are unavailable in this role, so scoped PowerShell reads were used. The role allowlist rejected Node reading and the clarification tool; CIM query was OS-denied. These did not authorize a bypass. apply_patch worked; no system package was installed.
- **Noticed, not done:** cross-stage pipeline work remains RES-2; the pre-encode callback requires the scope decision above, not a new deadline/configuration design.

## Worker completion

Scope-extension continuation approved 2026-11-18: the previous remaining item below is now authorized; its implementation/results follow in the next section.

- [x] Changes: Completed the earlier resume/order/async teardown fixes and the approved k1_embed.py/model_image.py extension: dedicated DecodeStopped, worker-local stop checks before open/implicit and explicit load/encode, and no result/checkpoint/refusal for stopped work. Added caller sweep, frozen byte/info oracle, stop regressions and updated Configuration audit/Completion report.
- [x] Checks: Latest approved-extension run: 21 files, 2120 passed, 40 skipped, through test_slot.py one pytest process at a time; 100-example absent-callback differential and both unchanged reviewer tests passed. Both new stage stops were red before the fix. Six extension mutants killed; prior six-mutant/19-file evidence remains separately recorded. Static verification and scoped git diff --check passed. Ruff attempts returned No module named ruff; no install.
- [x] Cleanup: Every owned test/mutant/verification child returned and was awaited; no live model/backend/GPU service started. Bounded loopback fixtures ended with pytest. Fixed basetemp, mutant sources and iteration/import-hook scripts removed; retain small command/PID/result reports and historical draft/proposal evidence only. No Git writes or secrets access.
- [x] Remaining: No implementation work or scope blocker remains from this brief. PM must obtain independent reviews and run the reserved full suite/test_kb_sync_flow.py/CI, review the approved scope decision with the user, and perform Git integration. Already-running native operations retain the accepted cpu_workers/pixel-cap residual and are awaited off-loop.

## Fix round 1 - approved pre-encode scope extension

### Step 0 (approved Small: yes continuation; R-017, R-018, R-020)

PM explicitly approved the proposal for k1_embed.py/model_image.py, replacing worker-thread CancelledError with dedicated DecodeStopped. No further approval gate. May assume existing lifecycle/resource bounds, same pixel policies and absent-keyword behavior, CPU synthetic doubles only. Must ask only if another semantic/configuration/scope change becomes necessary; none currently identified.

Unknowns checked before code: callbacks raised inside the broad file-error boundary could be misclassified; PNG EXIF access can itself load pixels; worker-local state must clear in finally, including stops; existing two-argument _decode wrappers must remain compatible. Native operations already running remain non-preemptible and awaited off-loop, bounded by cpu_workers and max_decode_pixels.

Plan: (1) freeze a labelled pre-change image-preparation oracle and add instrumented before-load/before-encode stops, show red; (2) add optional callbacks and DecodeStopped, worker-local binding/catch/cleanup, preserve all absent-keyword paths; (3) generated-image byte/info differential, every identified caller file, guards and focused mutants, then replace the stale blocked handoff and checked completion entries. Runtime files remain frozen throughout differential runs.

### Caller class sweep (pre-extension line references; source-verified by recursive src search and opening each caller)

| Caller / helper | File:line | Existing arguments / absent-keyword behavior | Verification |
|---|---|---|---|
| K1 image decode | k1_embed.py:383 | Shared vision/max_decode_pixels; only this path supplies worker stop callback after change; _decode(data, settings) stays unchanged | test_k1_embed, test_stage_flow, test_res1_resume/review_probes, new stop tests |
| S0 intake source image | intake.py:165 | prepare_vision_image_with_info(raw, vision, cap); no callback, same bytes/info/errors | all test_intake*.py |
| Model client role image | model_client.py:687 | prepare_image with resolved backend/role side, cap, alpha, format, quality; no callback | test_model_client, image_side, composition, cloud |
| Capability/production probe image | model_probe.py:136 | Same explicit image arguments through prepare_image; no callback | test_model_probe, test_model_production_probes |
| Reference and dev comparison source | embed_server/reference.py:198 | prepare_vision_image_with_info with K1 vision and cap; no callback | test_embed_server_reference, test_embed_server_dev_compare |
| Shared vision bytes helper | model_image.py:68 | prepare_vision_image -> prepare_image; no signature/default change | test_model_image, generated absent-keyword differential |
| Shared vision info helper | model_image.py:82 | prepare_vision_image_with_info -> prepare_image_with_info; optional callback forwarded, absent is None | same differential |
| Shared bytes helper | model_image.py:91 | prepare_image -> prepare_image_with_info; no signature/default change | same differential; test_k1_config directly calls info helper too |

### Configuration impact (before extension runtime edits)

No new key/schema/profile/default/deadline. Existing max_decode_pixels, image side/format/quality/alpha policy, batch_size, max_concurrency and cpu_workers remain validated by enhancer_config.v0 and existing read validators; base/dev examples unchanged. New optional check_stop=None is an internal callback API (not a deployment setting), preserving existing no-callback execution. DecodeStopped and worker-local event/attribute are safety-bound lifecycle control. Callback checks add no retries, model calls, precision/encoding or output changes. Test fault points, bounded waits and the frozen oracle are test-only. Existing override/refusal evidence plus all three guards will rerun; the row-by-row audit is filled at completion.

### Extension steps

1. In progress: caller sweep and frozen oracle, then red stop regressions; retain commands/results under .tmp/RES-1/ with extension-prefixed evidence.
   Research: repository image/error boundaries and every caller read; installed Python 3.12.10 _threading_local.py explains independent per-thread state. Pillow PNG load/EXIF behavior is checked in installed source before placing callbacks.
2. Done: boundary regressions were red (`2 failed, 3 deselected in 1.09s`), both observing an unwanted encode after stop; the frozen-oracle property first passed against the pre-change code (`1 passed, 4 deselected in 0.66s`). Implemented DecodeStopped (ordinary RuntimeError), optional callbacks outside file-error boundaries, worker-local catch/cleanup and stop-result guard.
   Done: the full new file passed (`7 passed in 1.36s`), including no checkpoint/refusal before load or encode, dedicated exception, no-task-cancel cooperative stop and independent callback threads. Additional check before PNG getexif is required by installed PngImagePlugin.py:1088-1092, which calls load when EXIF is absent.
3. In progress: serial caller/configuration/guard tests with frozen runtime source; then six focused scratch mutants and final report/audit/cleanup. No new keys or profile values.
   Evidence is distinct from earlier runs: extension-started.jsonl records every owned limiter PID immediately; extension-results.jsonl records commands/exits/results. The earlier blocked handoff is historical, superseded by this approved continuation.

### Extension research evidence

| Question | Source actually read / version | Raw or summary | Finding | Class |
|---|---|---|---|---|
| Per-worker cancellation context | Python 3.12.10 stdlib _threading_local.py:1-56 | Raw installed source documentation/examples | threading.local attributes belong to each thread; one thread's mutation is not visible in another. K1 sets/deletes its event in the worker and preserves the existing two-argument _decode API. | source-verified |
| Hidden pixel load during metadata | Pillow 11.3.0 PngImagePlugin.py:1088-1092 | Raw installed source | getexif calls load when exif is absent. Check stop before that call, then again before explicit source.load and before save. | source-verified |
| File/refusal boundary | model_image.py:35-57,118-182; k1_embed.py:381-436 | Raw repository source | Dedicated DecodeStopped bypasses file-error conversion; callback calls are outside file-byte blocks. Worker catches it as None, and a stop-result guard prevents SOURCE_CHANGED/refusal checkpoints. No asyncio.CancelledError raised by the worker. | source-verified |
| Default compatibility | tests/res1_image_oracle.py (frozen pre-edit transformation), test_res1_decode_stop.py | Actual CPU unit/property runs | 100 generated images compare exact bytes/info (or matching refusal) through all four preparation APIs with callback absent. Covers PNG/JPEG/TIFF/WEBP, RGB/RGBA, alpha flatten/refuse, EXIF 1/2/3/5/6/8, resize, quality, and exact/looser decode caps. Oracle first passed on old code; both stage stop cases first failed with an unwanted encode. | experiment |

### Configuration audit - approved scope extension

No new key, schema, profile or example change; no new deadline, retry, model/provider, precision or encoding policy. Existing configuration schema remains contracts/schemas/enhancer_config.v0.schema.json; base/dev examples remain unchanged. check_stop=None is an optional internal lifecycle callback: None means the existing caller supplies no stop source, not a fallback for missing deployment configuration.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Override / refusal tests run in this continuation | Retained-literal class |
|---|---|---|---|---|---|
| Optional stop callback and DecodeStopped | None: internal callable/exception, not a deployment value | Worker-local Event; callback raises dedicated ordinary exception; no checkpoint/result/refusal when stopped | None required; not a profile value | test_res1_decode_stop: before-load/before-encode, no-task-cancel stop, dedicated exception and independent threads | safety-bound |
| Decode pixel cap | vlm.backends.<embedder>.image.max_decode_pixels; vision.max_decode_pixels for S0; role backend cap for client/probe | Existing schema/read checks unchanged; shared image preparation still refuses an oversized image | Existing base/dev examples unchanged | test_model_image::test_decode_limit_and_bad_limits; generated exact/looser cap differential; test_k1_config; test_intake_config; test_field_contracts override/refusal | configurable |
| Image size/format/quality/alpha | vision.* and existing role/backend image settings | Existing image settings checks unchanged | Existing base/dev examples unchanged | test_model_image shared quality and alpha background override/refusal; test_model_client_image_side; all intake files; generated byte/info differential; test_field_contracts | configurable |
| CPU/request/batch resource bounds | runtime.cpu_workers, embedding.batch_size, vlm.backends.<b>.max_concurrency | Existing loader/read validation and pool/transport/flow caps unchanged | Existing base/dev examples unchanged | test_stage_flow capacity/worker override and refusal tests; test_k1_config; test_res1_resume workers/caps/batches and real repeated stop; test_field_contracts | configurable |
| Worker-local attribute event and no-result guard | None: lifecycle state, no material default | Cleared in finally; same stage Event passed to each worker; no source failure recorded for a cooperative stop | Not applicable | test_res1_decode_stop dedicated callback/context test and no-task-cancel stop; both real stage cancellations | safety-bound |
| Frozen oracle, image enums, EXIF values, bounded waits | None: tests only | Tiny synthetic CPU images and labelled hold/count doubles | Not applicable | 100-example no-callback differential; no live model/quality inference | test-only |

## Completion report

**Step 0 / authorization:** approved Small: yes scope-extension continuation above, R-017, R-018, R-020. This report supersedes the historical pre-extension BLOCKED handoff. PM's scope approval is recorded as a PM decision pending user review, per dispatch; implementation was explicitly authorized.

**Work completed:** model_image.py now defines ordinary module exception DecodeStopped and optional check_stop on the two info-producing helpers. It checks before open, before PNG EXIF access that can load pixels, before explicit load and before encode. Callback calls stay outside file-byte conversion blocks, and DecodeStopped is explicitly excluded from refusal conversion. Existing no-callback callers retain their arguments and behavior.

k1_embed.py binds the stage stop Event to each decode worker with threading.local, preserves _decode(data, settings), raises/catches DecodeStopped as no result, clears the context in finally, and declines to record any result/source failure/refusal when stopped. The existing awaited off-loop pool shutdown and resource bounds remain. The former pre-encode gap is closed.

New tests/test_res1_decode_stop.py instruments real decoding at before-load/before-encode boundaries, verifies no encode/checkpoint/refusal, verifies dedicated exception and independent callbacks, and tests a cooperative stop without task cancellation. New tests/res1_image_oracle.py is an explicitly test-only frozen pre-change transformation body; unchanged policy/metadata helpers are shared. Its 100-example property compares exact bytes and info, or the same refusal, through all four preparation APIs over formats, alpha, EXIF, sizes, quality and pixel caps. Caller sweep and row-by-row Configuration audit are above; no new key/profile/schema/example.

**Result and evidence (unit / mock_contract / property):** all 21 files below ran separately and serially through test_slot.py, no -n. Exact commands, PID ownership and exits: .tmp/RES-1/extension-started.jsonl and extension-results.jsonl. Verified totals and source hashes: .tmp/RES-1/extension-verification.txt. Total: **2120 passed, 40 skipped**. test_model_image also reports four existing Pillow I-mode PNG deprecation warnings; no policy/output was changed to suppress them.

Command form from this worktree: `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q tests/<one-file>.py --basetemp=<project>/.tmp/worktrees/RES-1/.tmp/RES-1/bt`. Environment: PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=this worktree's src. Owned children use CREATE_NO_WINDOW. Fixed basetemp removed after each run.

| Test file | Actual passing output |
|---|---|
| tests/test_embed_server_dev_compare.py | 71 passed in 20.56s |
| tests/test_embed_server_reference.py | 149 passed in 22.20s |
| tests/test_field_contracts.py | 502 passed, 34 skipped in 199.79s (0:03:19) |
| tests/test_intake_config.py | 31 passed in 7.46s |
| tests/test_intake_properties.py | 4 passed in 4.77s |
| tests/test_intake.py | 70 passed in 19.04s |
| tests/test_k1_config.py | 558 passed in 56.11s |
| tests/test_k1_embed.py | 68 passed, 1 skipped in 15.40s |
| tests/test_model_client_image_side.py | 36 passed in 8.54s |
| tests/test_model_client.py | 79 passed in 16.66s |
| tests/test_model_cloud.py | 10 passed in 0.20s |
| tests/test_model_composition.py | 54 passed in 44.90s |
| tests/test_model_image.py | 202 passed, 4 warnings in 0.66s |
| tests/test_model_probe.py | 5 passed in 0.14s |
| tests/test_model_production_probes.py | 84 passed in 13.63s |
| tests/test_property_settings.py | 1 passed in 2.00s |
| tests/test_res1_decode_stop.py | 7 passed in 1.36s |
| tests/test_res1_resume.py | 4 passed in 74.09s (0:01:14) |
| tests/test_res1_review_probes.py | 2 passed in 1.91s |
| tests/test_stage_flow.py | 24 passed in 34.32s |
| tests/test_storage_anchored_writes.py | 159 passed, 5 skipped in 9.01s |

**Red / green:** before runtime edits, both new stage stop cases failed with an unwanted encode: `2 failed, 3 deselected in 1.09s`. The frozen oracle first matched the original implementation: `1 passed, 4 deselected in 0.66s`. After the change the expanded stop/property file passed all seven tests. Full caller files, all three guards, stage-flow differentials, the original unchanged reviewer tests and interrupted-run properties passed on frozen final runtime source. No live model/quality/throughput claim; HTTP tests use labelled in-memory or bounded loopback doubles only.

**Extension mutants:** six non-equivalent scratch-only substitutions, one file/process through the limiter, all killed by the intended failed test rather than collection errors. Live source SHA-256 was checked unchanged after each run. Commands/PIDs/results: .tmp/RES-1/extension-mutants.json; outputs: extension-mutant-*.log. No timeout/crash counted as a kill.

| Mutation | Actual result |
|---|---|
| Do not forward K1 stop callback | killed; 1 failed, 5 deselected in 1.01s |
| Remove pre-encode stop check | killed; 1 failed, 6 deselected in 0.93s |
| Remove checks before implicit/explicit pixel load | killed; 1 failed, 6 deselected in 1.01s |
| Convert DecodeStopped into file refusal | killed; 1 failed, 6 deselected in 0.60s |
| Do not handle DecodeStopped in worker | killed; 1 failed, 6 deselected in 1.25s |
| Record stopped None as SOURCE_CHANGED | killed; 1 failed, 6 deselected in 1.07s |

**Important change/reason:** the proposal's asyncio.CancelledError from the worker was replaced exactly as PM directed; cancellation of the async stage still uses normal asyncio cancellation. The extra callback before getexif is necessary because installed Pillow PNG code performs an implicit load there. No deadline, killable process, global callback, schema or deployment setting is introduced. Already-running OS/native operations are still not preemptible; at most cpu_workers run, each under max_decode_pixels, and all are awaited off-loop.

**Checks / not_run:** syntax and whitespace checks passed; scoped git diff --check returned 0 with no output. Ruff check and format were attempted on k1_embed.py, model_image.py, test_res1_decode_stop.py and res1_image_oracle.py, both returned exit 1 with exact output below. No install attempted.
```text
<project>\.venv\Scripts\python.exe: No module named ruff
ruff check exit=1
<project>\.venv\Scripts\python.exe: No module named ruff
ruff format exit=1
```
Full suite, test_kb_sync_flow.py, CI and live model/GPU/deployment runs are not_run (PM-owned or explicitly prohibited). No external network, model inference, downloads, installs, Git writes or secrets access. New tests use tiny generated images only.

**Changed files in this continuation:** src/prompt_enhancer/k1_embed.py, src/prompt_enhancer/model_image.py; new tests/test_res1_decode_stop.py and tests/res1_image_oracle.py; this task file. All predecessor PM/RES-1 edits remain unstaged and untouched outside this scope.

## Handoff

- **Current status: REVIEWING — implementation and required verification complete.** The prior pre-encode scope blocker is resolved; no implementation defect remains open from this brief.
- **Already done:** Step 0/caller sweep; dedicated cooperative exception and callbacks; red/green boundary tests; 100-example default byte/info differential; 21 passing files (2120 passed, 40 skipped); 6/6 extension mutants killed; row-by-row Configuration audit with no new key. Detailed primary-source evidence and source hashes are retained above/in extension-verification.txt.
- **Next PM action:** obtain the requested independent reviews, then run reserved integration/full-suite/CI and handle Git delivery. Review the scope decision with the user as the dispatch requires. No further scope approval is required for this implemented patch.
- **Do not repeat:** never apply the old .tmp/RES-1/pre-encode-scope-proposal.patch; it is historical and used CancelledError, superseded by the checked implementation. Do not edit source during differential runs, introduce deadlines/configuration keys, abandon threads, or run a full suite in this worker.
- **Cleanup/lifecycle:** all owned test/mutant/probe children returned and were awaited; no live model/backend/GPU service started. Bounded loopback test fixtures ended with their pytest processes. Fixed basetemp and mutant source are absent; extension runner/import-hook/verification scripts are removed before final handoff. Retain only small reports, exact-command/PID/result logs and historical proposal/draft evidence in .tmp/RES-1/.
- **Configuration audit:** the extension table above covers every changed operational behavior and retained literal; existing caps/policies keep their validated keys and unchanged examples, with override/refusal evidence from the rerun caller/config/guard files.
- **Noticed, not done:** four existing Pillow I-mode PNG deprecation warnings remain; changing that encoding behavior is outside scope and could alter bytes. Cross-stage pipelining remains RES-2. No new lesson beyond the freeze-source and unique property-fixture rules already recorded.

## Fix round 2

### Step 0 (Small: yes; implementation authorized by PM dispatch)

R-IDs: R-017, R-020. Goal: no replayed image/text chunk writes, fails or reports a member already in records. Scope: k1_embed.py, listed tests, this task file. May assume accepted full-membership replay, fatal-drain/stop semantics, CPU synthetic doubles, unchanged configuration. Must ask only for an additional semantic/configuration/scope change; none identified. No Git writes, services, installs, network, GPU or sub-agents.

Unknowns/failure modes: image source-verification and refusal outcomes lack the existing success/HTTP-error membership guards; full-member replay must still include prepareable done companions for batch-sensitive equality. A changed/unprepareable companion must leave the request while pending members continue. Cached refusals and initial skip/reuse accounting remain authoritative. Verify both image/text write paths and helpers, not just the reproduced branch. Freeze runtime source during differential tests.

Questions: none; the dispatch explicitly resolves changed-source semantics and authorizes this small continuation.

Plan and finish: (1) extend interrupted-run properties with changed/undecodable done companions, record real red before runtime edits; (2) add the missing preparation guard and enumerate every write/helper guard; (3) run eight required files separately through test_slot.py, 3-5 scratch-only mutants, scoped lint/whitespace checks, Configuration audit and handoff. Finish only with red/green evidence, real per-file output, mutation outcomes and cleaned owned scratch.

### Configuration impact (before runtime edit)

No new key, schema, profile, example, literal or hidden default. The change guards existing artifact/report mutations using records membership. embedding.batch_size still determines complete-input chunk membership; backend max_concurrency and runtime.cpu_workers retain the existing flow/transport/pool bounds. sync.resume and configured vision/decode policy are unchanged. Existing validation: embedder_settings -> validate_embedder_reads/_validate_embedding, _cpu_workers -> resolve_workers; schema enhancer_config.v0.schema.json; profiles enhancer.example.yaml and enhancer.dev.example.yaml. Required field-contract override/refusal tests and stage-flow capacity/worker tests will run. Changed-source requests may shrink: R-017 same-input equality excludes changed source/availability; pending members still run.

### Research evidence

Source-verified: read Fix round 1, the Claude rev2 review and reproducer description, k1_embed.py preparation/success/error/text paths and local storage Checkpoints.commit/fail/load. No external API change or new algorithm. The current storage _transition already refuses demotion of a done row; the K1 failure call/report is still wrong, and a refusal commit can replace the vector. Assert no calls for done keys, exact persisted rows/artifact bytes and returned vectors, rather than infer behavior from the older review's storage version. rg/Read/Grep are unavailable; scoped PowerShell reads used. No test or runtime edit yet.

### Steps

1. Done: added generated partial-commit boundaries (batch 2-4, caps 1-3, workers 1/3), changed/corrupted source files and a selective labelled decoder-refusal double. Exact done rows/artifact bytes, no commit/fail calls, vectors and pending execution are asserted.
   Red before runtime edit: `3 failed, 4 deselected in 90.63s (0:01:30)`; .tmp/RES-1/fix2-red.log and fix2-results.jsonl. The changed/corrupt cases called fail on a done key; decoder refusal overwrote its vector record.
2. Done: guard unavailable/refused outcomes for records members before any preparation mutation. Successful companions remain in the request; all existing image/text success/error guards stay intact. The class table below enumerates every mutation and helper.
   Green: all eight required files, 770 passed / 40 skipped, frozen runtime source, one file/process at a time; exact commands/PIDs/exits in .tmp/RES-1/fix2-started.jsonl and fix2-results.jsonl.
3. Done: four scratch-only guard mutants killed; the missing-membership mutant initially survived a differential comparison, then the existing explicit source-failure oracle killed it. No production source edit or test weakening.
   Completed audit and handoff below; scoped lint attempts reported not_run because ruff is absent. Final static verification and owned scratch cleanup: .tmp/RES-1/fix2-verification.txt.

### Class sweep - every image/text replay write and helper (final runtime lines)

All references below are src/prompt_enhancer/k1_embed.py unless stated. Helpers do not know records membership; their caller guards are the invariant. Successful prepared done companions remain batch context. A done companion that cannot be prepared leaves the request without any mutation; pending members still run. The resulting request can differ from an uninterrupted run because source/decoder availability changed, outside the accepted same-input R-017 equality boundary.

| Write / helper | File:line | Guard / reason |
|---|---|---|
| Image source failure checkpoint | k1_embed.py:471 | New :468-469 skips an unavailable outcome when image_id is in records; only missing members reach fail. |
| Image SOURCE_CHANGED report entry | k1_embed.py:472 | Same preparation guard :468-469. |
| Image refusal artifact via _commit_record | k1_embed.py:478 | New :468-469 skips a refused outcome when image_id is in records. |
| Image refusal records entry | k1_embed.py:479 | Same preparation guard; only a missing member is added. |
| Image refusal decode_failed entry | k1_embed.py:480 | Same preparation guard; done vectors cannot become refusals. |
| Image HTTP/response failure checkpoint | k1_embed.py:491 | Existing if image_id in records: continue at :489-490. |
| Image HTTP/response failed report entry | k1_embed.py:492 | Same error guard :489-490. |
| Image vector artifact via _commit_record | k1_embed.py:500 | Existing if image_id in records: continue at :495-496. |
| Image successful records entry | k1_embed.py:501 | Same success guard :495-496. |
| Image embedded counter | k1_embed.py:502 | Same success guard :495-496. |
| Text HTTP/response failure checkpoint | k1_embed.py:573 | Existing if text in records: continue at :571-572. |
| Text failed report entry | k1_embed.py:574 | Same error guard :571-572. |
| Text vector artifact via _commit_record | k1_embed.py:582 | Existing if text in records: continue at :577-578. |
| Text successful records entry | k1_embed.py:583 | Same success guard :577-578. |
| Text embedded counter | k1_embed.py:584 | Same success guard :577-578. |
| _commit_record -> checkpoints.commit | k1_embed.py:364-365 | All three callers enumerated above are guarded. No helper-side mutation or fallback. |
| checkpoints.fail -> _transition | storage/store.py:1201-1214 | Caller guards above prevent even a fail call for done companions. Current storage also independently preserves done state; it cannot prevent the erroneous K1 report entry. |
| Initial cache records / cached refusal reporting | k1_embed.py:450,452,562 | Outside replay; once per sorted unique input, only after _skip_or_reuse returns an intact record. Cached refusal reporting is intentional semantic reconstruction, not a repeated replay result. |
| _skip_or_reuse skip/reuse accounting | k1_embed.py:356,359 | Outside replay; done(key)/reusable(key) branches only, once per input. No checkpoint write. |
| Capacity report | k1_embed.py:513,588 | Stage telemetry only; not a member outcome. |
| failed/decode_failed map sorting | k1_embed.py:517-518,590 | Canonicalizes existing entries; adds no member. |

Wholly cached chunks return at :455-456 (images) and :565-566 (texts). The stop guard at :463-464 precedes every preparation write. Building prepared and request lists changes no member record/report. Final tables/metadata use authoritative records; they do not mutate checkpoints or count members again.

### Configuration audit - fix round 2

No new key/schema/profile/example. No model/provider/precision/image policy/path/timeout/retry/deadline/lifecycle change. Existing validated values are still declared by contracts/schemas/enhancer_config.v0.schema.json and configs/enhancer.example.yaml / enhancer.dev.example.yaml. No runtime literal was added; the new membership condition is an artifact-state invariant. The test-only generated values/fault points remain in tests. Results for every named file are recorded in the Completion report below.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override / refusal evidence | Retained-literal class |
|---|---|---|---|---|---|
| Immutable done companions / missing-only outcomes | Existing done/reusable artifact records, no setting | Existing intact-record verification; new guard :468, existing write guards in class table | No new profile value | test_k1_interrupted_done_companion_is_immutable: changed bytes, corrupt bytes, selective decode refusal; exact state/artifact/vector preservation and zero done-key write calls | artifact-bound |
| Complete-input chunks / changed companion omitted | embedding.batch_size, unchanged | validate_embedder_reads / _validate_embedding; existing schema | Both existing examples unchanged | test_k1_embed::test_batch_size_override uses 3, requests [3,2]; interrupted properties vary 1-4 and verify pending members continue; test_field_contracts::test_config_override_and_refusal[embedding.batch_size] | configurable |
| Request capacity, unchanged | vlm.backends.<b>.max_concurrency | Existing positive integer/backend validation and transport bound | Both existing examples unchanged | test_stage_flow::test_k1_images_concurrent_equal_serial_with_same_batches and test_k1_texts_concurrent_equal_serial compare 1/3; new property caps 1-3; test_field_contracts::test_config_override_and_refusal[vlm.backends.main.max_concurrency] | configurable |
| Decode workers, unchanged | runtime.cpu_workers | _cpu_workers -> resolve_workers before reads | Existing auto value unchanged | test_stage_flow::test_k1_decode_pool_follows_cpu_workers (3); test_k1_refuses_an_unusable_cpu_worker_count_before_any_read (0/True/sentinel); field-contract override/refusal[runtime.cpu_workers] | configurable |
| Resume eligibility, unchanged | sync.resume | Existing schema and Checkpoints.done/reusable | Both existing examples unchanged | test_k1_embed::test_resume_false_recomputes_an_unreferenced_done_artifact and test_resume_false_reuses_referenced_artifacts_only; field-contract override/refusal[sync.resume] | configurable |
| Existing decode policy / refusals | vision.* and backend image.max_decode_pixels, unchanged | Existing embedder read/image validators | Both existing examples unchanged | test_k1_embed policy refusal and malformed-image/parser properties; field-contract override/refusal; decode-stop absent-callback differential remains green | configurable |
| Member report outcomes / ordering | Existing failed/decode_failed/embedded protocol | Missing-only caller guards; canonical map sorting unchanged | No new field | New property checks empty failed/decode_failed and exact embedded/skipped counts; test_stage_flow::test_failure_maps_sorted_under_reversed_completion; existing cached-refusal property | protocol-bound |
| Generated values / faults / write spies | Tests only | Tiny synthetic images, bounded CPU cases, labelled decoder/server doubles | Not a profile value | New property: batches 2-4, partial boundary, caps 1-3, workers 1/3; scratch mutants use concrete cases of the same assertions | test-only |

### Completion report - fix round 2

**Work completed (R-017, R-020):** k1_embed.py:468 skips an unavailable/refused preparation outcome when the member is already in records. It leaves that member's done checkpoint, artifact bytes, vector, metadata and failure reports intact while embedding pending members. Prepareable done companions still supply full batch context and are not rewritten by the existing success/error guards. Both text guards were already correct. The class table above inventories every member write, helper call and report update, including initial cache accounting and stage telemetry outside replay.

tests/test_res1_resume.py adds test_k1_interrupted_done_companion_is_immutable: generated batch sizes, partial commit boundaries, capacities and worker counts; actual changed valid bytes, actual corrupted file bytes, and a labelled decoder double that refuses the same verified companion bytes. It compares exact persisted rows/artifact bytes and returned done vectors, spies on commit/fail, checks no failed/decode_failed outcome, exact invocation counts and continued pending execution. The latter double isolates the refusal branch, since corrupting bytes normally fails content verification first.

**Result and evidence (CPU unit/mock_contract/property/replay, no live inference):** before runtime edit the new cases were red: `3 failed, 4 deselected in 90.63s (0:01:30)` (.tmp/RES-1/fix2-red.log). The changed/corrupt cases called fail on done keys; the refusal case replaced the vector artifact. After the guard, all eight required files passed separately and serially through test_slot.py on frozen runtime source. Total **770 passed, 40 skipped**; no skip is counted as a pass.

Exact commands, PIDs, exit codes and actual summaries: .tmp/RES-1/fix2-started.jsonl and fix2-results.jsonl. Command form from this worktree: `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q tests/<one-file>.py --tb=short --basetemp=<project>/.tmp/worktrees/RES-1/.tmp/RES-1/fix2-bt`. The red run additionally selects `-k interrupted_done_companion`. Environment: PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=this worktree's src; owned child launchers use CREATE_NO_WINDOW; no -n. The fixed basetemp was deleted after each completed command.

| Required file | Actual passing output |
|---|---|
| tests/test_res1_resume.py | 7 passed in 74.52s (0:01:14) |
| tests/test_res1_review_probes.py | 2 passed in 1.33s |
| tests/test_res1_decode_stop.py | 7 passed in 1.42s |
| tests/test_k1_embed.py | 68 passed, 1 skipped in 18.53s |
| tests/test_stage_flow.py | 24 passed in 31.20s |
| tests/test_field_contracts.py | 502 passed, 34 skipped in 211.68s (0:03:31) |
| tests/test_storage_anchored_writes.py | 159 passed, 5 skipped in 14.33s |
| tests/test_property_settings.py | 1 passed in 5.93s |

**Mutants:** four non-equivalent substitutions of the new guard, scratch module only, loaded through a scratch import hook; original __file__ retained so source-hashed checkpoint identity uses the real dependency tree. Runtime SHA-256 stayed unchanged: `6649644d558aa4ebd73830f33728d1fd54aa0caac84fdba208e8a87365c8ea54`. Concrete generated cases call the same property assertions without Hypothesis shrinking (batch=3, cap=2, workers=1); no assertions removed. Exact commands/results: fix2-results.jsonl; mutation definitions/outcomes: fix2-mutants.json; assertion traces: fix2-mutant-*.log.

| Guard mutant | Outcome / actual output |
|---|---|
| Protect refusals but allow unavailable done sources to fail | killed by new property write-call assertion; 1 failed in 1.51s |
| Protect unavailable sources but allow done decode refusal overwrite | killed by new property exact row/artifact comparison; 1 failed in 1.23s |
| Remove membership condition, suppress pending preparation failures too | initially survived old clean/resume differential: 1 passed in 1.66s; killed by existing test_k1_embed::test_changed_source_fails_then_recovers requiring SOURCE_CHANGED: 1 failed in 1.11s |
| Skip all done outcomes, including prepareable batch companions | killed by existing interrupted property vector equality: 1 failed in 2.08s |

No timeout/crash/collection failure was counted as a kill. The initial mutation-report parser required the literal AssertionError; pytest abbreviated the dictionary failure as `E   assert`, so that legitimate kill was first misclassified. Read the trace, corrected the scratch evidence parser and reused its completed result without rerunning it. The surviving differential case is retained and explained, not silently discarded.

**Important changes and reasons:** one preparation guard covers both missing-source and refusal writes. No extra abstraction, recovery artifact, configuration or shared-storage change. Current storage already blocks done-state demotion, but cannot stop a false K1 failed report or a refusal artifact overwrite; caller-side guards are required. A done companion that cannot be prepared leaves the request, so its pending members may have different batch-sensitive vectors from an uninterrupted run after source/decoder availability changes; this is outside the accepted same-input R-017 equality boundary. Unchanged prepareable companions remain in the batch. The usual source/tool revision re-key remains the already accepted integration behavior.

**Configuration audit:** completed row by row above; no new key/schema/profile/example/default/literal. All cited file-level property, override/refusal and guard checks ran. The existing configuration tests cover the same resource/decode/resume settings; their behavior is unchanged.

**Checks / not_run:** final syntax/source-hash/evidence/whitespace verification is retained in .tmp/RES-1/fix2-verification.txt; scoped git diff --check returned 0 with no output. Ruff check and format were both attempted on src/prompt_enhancer/k1_embed.py and tests/test_res1_resume.py, both exit 1, neither check ran. Exact output for each:

```text
<project>\.venv\Scripts\python.exe: No module named ruff
```

No install attempted. Full suite/test_kb_sync_flow.py/CI and independent retest are not_run here (PM-owned). Live model/GPU/network/downloads are prohibited and not_run. No new required implementation issue remains. No Git write, secrets access, database write or models write occurred.

**Changed files this round:** src/prompt_enhancer/k1_embed.py, tests/test_res1_resume.py, tasks/active/RES-1-resource-use.md only. Pre-existing unstaged PM/RES-1 work remains untouched. Small evidence stays under .tmp/RES-1/fix2-*; owned launcher/import-hook/mutant/static-verification scripts and mutant module are removed; every owned child returned and was awaited.

**Lessons for PM:** a clean/resumed differential can miss a defect shared by both runs; keep explicit completeness/failure oracles alongside it. Mutation classifiers must recognize pytest assertion rewriting as well as literal AssertionError, and retain the real trace.

### Handoff - fix round 2

- **Current status: REVIEWING.** Scoped implementation and required checks complete, awaiting independent Claude Sonnet retest/PM integration.
- **Already done:** Step 0, red/green property, complete image/text/helper write sweep, no-new-key audit, eight passing files (770 passed / 40 skipped), four non-equivalent mutants killed, frozen source and whitespace verification, owned scratch cleanup.
- **Next PM action:** obtain independent retest, run reserved integration/full-suite/CI and handle Git delivery. Keep the changed-input equality boundary explicit.
- **Do not repeat:** no missing-only request replay for unchanged prepareable companions, no done-key failure/refusal writes, no source edits during differential runs, no live model/GPU work or worker Git writes. Do not apply historical pre-encode proposal patches.
- **Worktree/base/partial patch:** existing RES-1 worktree and unstaged full patch from the dispatch; the worker made no Git writes. No separate partial patch needed.
- **Lifecycle:** all owned test/mutant children awaited, fixed basetemp removed after each run; no server/service started. Only small logs/results/hash report retained for this round.
- **Noticed, not done:** no additional implementation issue. Existing RES-2/native-operation residuals remain outside this brief.

## Fix round 3

### Step 0 (Small: yes; implementation authorized by PM dispatch)

R-IDs: R-017, R-018, R-020. Goal: deterministic test concurrency evidence independent of item/source-hash order, plus a text replay success-guard property. Scope: tests identified by the sweep and this task file. Runtime source is read-only. May assume existing runtime behavior, CPU synthetic/standard bounded loopback fixtures and accepted replay semantics; no new deployment configuration. Must ask/stop if a runtime defect or another scope/semantic decision appears. No questions currently: the dispatch identifies both test defects and authorizes this continuation.

Unknowns/failure modes: gating every request wave can deadlock on the final partial wave, cached/resumed work or sequential probes; use a one-shot gate only where capacity is measured and derive its target from independently known pending work. A timed sleep/yield alone is not capacity evidence. Upper-bound and explicit fake-clock/state assertions need different explanations from reachability claims. A text replay must check physical commit calls/rows and invocation counts, because semantic vector equality alone missed the success-guard mutant.

Plan/finish: (1) enumerate all concurrency peak/count assertions and request-release mechanisms; (2) add bounded arrival/observation gates where needed and the text interrupted-run property, show the guard-removal scratch mutant red; (3) required files separately through test_slot.py, stage-flow twice plus scratch item reordering, 2-4 mutants, no-new-key audit and report/cleanup. Runtime bytes stay frozen throughout.

### Configuration impact

Test-only continuation: no runtime source/behavior, configuration key, schema, example/profile, provider/model, resource limit, image policy, retry/deadline or lifecycle setting changes. New gate targets and timeout bounds are explicitly test-only synchronization/failure controls, derived from each fixture's configured cap and pending work; they are not runtime barriers. Existing field-contract override/refusal checks and resource/parser properties will run.

### Steps

1. Done: recursively inventoried peak/in-flight/active/handler assertions, including dynamic composition assertions, fake-clock/state checks and native-thread holders; class table and static cross-check retained below.
   Read the Claude round-2 P2/P3 reproducers and every changed helper/caller. No runtime defect identified; runtime source remains untouched.
2. Done: one-shot bounded arrival gates for HTTP/model/lookup doubles; native decode Barrier; sampler acknowledgment; asserted composition Event wait; text replay property for success and HTTP failure.
   Scratch red: removed success guard, 1 failed in 14.94s; removed error guard, 1 failed in 12.18s; gate forced open, 1 failed in 0.22s. Normal resume file green: 9 passed in 66.36s.
3. Done: every required/changed test file passed; stage-flow normal passes are 24 passed in 26.16s and 24 passed in 28.60s; the full scratch reversed-order run passed 24 in 28.99s.
   All runtime source hashes checked unchanged; final audit/report below and final syntax/evidence/cleanup verification in .tmp/RES-1/fix3-verification.txt. No implementation issue remains open from this continuation.

### Class sweep - concurrency assertions across tests (final test lines)

Read raw test sources, helpers and callers after recursive text search; static AST cross-check and exact test names are retained in .tmp/RES-1/fix3-assertion-inventory.txt. All paths below are under tests/. ArrivalGate is a labelled test-only one-shot gate: count arrivals while requests are held, release when the independently known target is reached, fail with AssertionError within five seconds otherwise. It stays open for later requests and partial tails. Only fresh-work capacity measurements enable a multi-arrival HTTP gate; resume/stop helpers do not assume their remaining request count. Assertions of a positive peak CAP below all have independently known requests >= CAP; capacity telemetry additionally asserts peak == min(cap, requests) in test_stage_flow.py:153. No item or checkpoint-hash ordering chooses a release.

| Test / assertion location | Double release / deterministic reason | Change |
|---|---|---|
| test_stage_flow.py:205, K4 differential | Flight holds min(cap, len(names)) first requests; measured fresh inputs include ten names. | Explicit arrival gate, opt-in measurement; cached/fatal/stop runs keep their prior semantics. |
| test_stage_flow.py:331, K3 differential | Flight holds min(cap, concept count); six fresh concepts each call once. | Arrival gate before seeded delays. |
| test_stage_flow.py:369, K2 observation differential | Flight holds min(cap, 8), four fresh folders x two selected representatives; queued-only tasks can finish and replenish flow independently. | Arrival gate before seeded delays; no sorted-key assumption. |
| test_stage_flow.py:437, K2 naming differential | Flight holds min(cap, 9), nine fresh folders; each can issue an independent first assessment/naming call before dependent draws. | Gate enabled only for the fresh measurement, never inferred for resume. |
| test_stage_flow.py:506, K1 image differential (both failure parameters) | Flight holds min(cap, 4); seven valid images in size-two chunks imply at least four requests, irrespective of two refusal positions. | Arrival gate before delay/failure; no source-hash order dependency. |
| test_stage_flow.py:546, K1 text differential | Nine unique texts in size-two chunks yield five requests; gate holds min(cap, 5). | Arrival gate before seeded delay. |
| test_stage_flow.py:592, K1 decode workers | Three real native-worker wrappers hold at threading.Barrier(3, timeout=5) before decoding. | Replaces a 50 ms sleep; failure breaks the barrier, not a hang. |
| test_stage_flow.py:625, S1 differential | SlowEncoder holds min(cap, 8) first batch calls; fixture independently builds eight query batches. | Arrival gate before seeded delay. |
| test_stage_flow.py:659, S1 failure differential | Same eight-batch gate; the content-chosen failure happens only after arrival release. | Same gate; no failure-position timing requirement. |
| test_stage_flow.py:718, S2 differential | Lookup wrapper holds one serial call or all four concurrent mentions before doing the lookups/failure. | Arrival gate before seeded delay. |
| test_stage_flow.py:153, check_capacity helper | Called by K1/K2/K3/K4 differentials above; telemetry peak == min(cap, actual returned requests). | Reachability supplied by the same HTTP gate; fake latency is not capacity evidence. |
| test_model_resources.py:108, concurrency/cancellation | Eight calls contend for cap two; first two hold at ArrivalGate before the existing cancellation delay. | Explicit two-arrival gate. |
| test_model_resources.py:161, capacity integration | FakeClock, explicit awaited enter/exit steps for a/b/c; no scheduling/timer race. | Unchanged, already deterministic. |
| test_model_resources.py:181-182, window mid-flight | One slot entered before opening the fake-clock window, then explicitly exited and window closed. | Unchanged, already deterministic. |
| test_model_resources.py:202, configured capacity (1/3) | Three times limit calls; gate holds limit calls before any finishes. | Arrival gate replaces a 10 ms sleep. |
| test_model_resources.py:234, cancelled slot waiter | Test owns the only slot while it starts/cancels/awaits the waiter, then exits it. | Unchanged, explicit slot ownership. |
| test_model_resources.py:347,350, flow differential property | flow creates min(limit, input count) tasks before waiting; each generated delay is >=1 explicit sleep(0) suspension. Thus every initial task starts before any can finish, independent of item identity; failure case asserts only the upper bound. | Unchanged structural scheduler proof, not wall-clock delay. |
| test_model_resources.py:380, flow stop property | Parent cancels then awaits the flow task and all owned children; asserts zero residual flight. | Unchanged explicit cleanup completion. |
| test_k2_observe_edges.py:81, backend limit | Four selected images, cap two; first two HTTP handlers hold at gate; decrement in finally. | Replaces a lone sleep(0). |
| test_k3_edges.py:167, selected backend limit | Two concepts, caps one/two; each fresh double holds min(cap, 2) calls at its gate; finally decrements. | Replaces a lone sleep(0); fresh semantic inputs remain. |
| test_k4_aliases.py:410, concurrency/close (success/error/cancel) | Three concepts, cap two; both calls arrive before entered is signalled. Success/error then continue; cancel waits for parent cancellation. | Arrival gate before signalling cancellation readiness, finally clears activity. |
| test_model_composition.py:324, loaded profile concurrency case | Three HTTP handlers increment under a lock; Event set by the third arrival. Parent asserts returned actual concurrency equals configured value three. | Existing hold retained; its bounded wait is now asserted, so timeout fails instead of releasing silently. |
| test_embed_server_http.py:236, GPU lock serialization | Four completed requests enter one forward lock; Counting counts inside that protected critical section, so observed peak is exactly one regardless of order; a small engine delay is supplemental contention, not the reachability determinant. | Unchanged upper-bound/serialization assertion, source-inspected only this round. |
| test_embed_server_lifecycle.py:122,129,141, connection cap (2/4) | hold_deadlines keeps accepted readers alive; bounded wait observes exactly cap handlers; explicit expire and socket closes release them. | Unchanged, already held to observation. |
| test_embed_server_lifecycle.py:173,175,181, slow connection cap | Reader deadlines held; sampler explicitly sets at_capacity when it sees configured cap; parent waits for that acknowledgment before expire. | Replaces a 500 ms observation sleep with an asserted Event wait. |
| test_embed_server_lifecycle.py:219,225, stop joins handlers | Five known idle/partial-upload sockets; bounded wait observes five, stop closes/joins all, then zero is asserted. | Unchanged explicit ownership/stop boundary. |
| test_embed_server_lifecycle.py:329, unfinished forward stop | Recording engine gate holds native forward until parent releases in finally; bounded cleanup wait observes zero handlers. | Unchanged explicit release. |
| test_embed_server_lifecycle.py:413, handler held at stage | hold_at stage Event acknowledges entry, stop reports still-held handler/admission, finally releases hold, bounded zero-handler wait. | Unchanged explicit release. |
| test_embed_server_lifecycle.py:454, CLI held-stage exit | Same hold_at Events; child command stop completes/fails, parent releases, bounded cleanup wait observes zero. | Unchanged explicit release. |
| test_embed_server_lifecycle.py:490, empty stop state | No handlers; synchronous finish and registered/working/in-flight/engine state inspected after completion. | Unchanged direct state assertion. |
| test_embed_server_lifecycle.py:500, admission slot error | Test assigns in_flight/working state directly, no request double or scheduler involved. | Unchanged direct-state error-format assertion. |
| test_embed_server_lifecycle.py:580, closing admissions | Explicit admit/work_started/work_done/release/begin_closing sequence; count zero after release. | Unchanged direct state assertion. |
| test_embed_server_lifecycle.py:647, discard budget cleanup | Synchronous upload-until-close, bounded drain/zero-handler waits. | Unchanged cleanup count, no peak claim. |
| test_embed_server_lifecycle.py:954, full log pipe stop | hold_deadlines/hold owns one accepted reader; stop acknowledged before asserting all handlers joined. | Unchanged explicit lifecycle boundary. |

Search exclusions checked: test_embed_server_dev_compare.py:199 is a tracemalloc memory peak; input_counts assertions count batch membership, not simultaneous requests; test_embed_server_config.py:77 validates configured max_pending_requests, not observed capacity; model ledger _active emptiness checks track budget tokens, not concurrency. These require no arrival gate. Unchanged cleanup assertions above are not throughput/reachability measurements.

### Research evidence - fix round 3

| Question | Source actually read | Finding | Class |
|---|---|---|---|
| P2/P3 mechanisms | .tmp/RES-1/review2-claude.md findings; raw stage/resource/edge/server/composition tests; k1_embed.py text success/error paths; k2_observe.py task selection/flow | Peak evidence previously relied on completion timing. Text semantic differential omitted physical rewrite/accounting assertions. Runtime already has both guards. | source-verified |
| One-shot async release | Installed Python 3.12.10 Lib/asyncio/locks.py:156-219; existing tasks.py cancellation/wait_for source evidence from fix round 1 | Event.set wakes waiters and stays set; timeout raises, rather than opening a partial wave silently. Gate is test-only. | source-verified |
| Native-worker rendezvous | Installed Python 3.12.10 Lib/threading.py, Barrier/wait implementation and docstrings | Threads wait for all parties; explicit timeout breaks the barrier. Three configured workers and three inputs make target independent of scheduling. | source-verified |
| Text guard oracle | New test_res1_resume.py::test_k1_interrupted_done_text_is_immutable; scratch red logs | Removing success guard produces a done-key commit; removing error guard produces a done-key fail call. Exact persisted rows and invocation counts are checked, not only vectors. | experiment |

### Configuration audit - fix round 3

No runtime change and no new key/schema/profile/example/default. Production settings and source bytes are unchanged. Schema/profile ownership remains contracts/schemas/enhancer_config.v0.schema.json and configs/enhancer.example.yaml / enhancer.dev.example.yaml. Existing field-contract override/refusal checks run unchanged; this continuation changes how tests substantiate those resource values, not what they mean.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Override/refusal or invariant evidence | Retained-literal class |
|---|---|---|---|---|---|
| Request capacity evidence | Existing vlm.backends.<b>.max_concurrency, unchanged | Existing runtime loader/flow/transport validation unchanged | Existing examples unchanged | Stage-flow cap 1/3 paired results, K2/K3/K4 edge cap 1/2, model_resources limits 1/3, unchanged test_field_contracts::test_config_override_and_refusal | configurable (unchanged) |
| Worker capacity evidence | Existing runtime.cpu_workers, unchanged | Existing resolve_workers refusal at stage entry unchanged | Existing auto profile unchanged | Stage-flow three-worker barrier/three-image fixture; existing 0/True/sentinel refusal cases; field-contract override/refusal | configurable (unchanged) |
| Gate targets / five-second rendezvous and sampler bounds | No deployment field; fixture pending-work and configured-cap values only | ArrivalGate AssertionError on an unfulfilled target; native Barrier timeout; asserted threading.Event waits | Not a profile value | test_capacity_arrival_gate_is_bounded_and_stays_open; all class-table capacity assertions; scratch forced-open mutant killed | test-only |
| Text batch/replay eligibility | Existing embedding.batch_size and sync.resume, unchanged | Existing read/schema/record verification unchanged | Existing examples unchanged | New text property varies batch 2-5, partial boundary and cap 1-3; existing field-contract override/refusal | configurable (unchanged) |
| Immutable done text / invocation accounting | Existing done artifact records, no new setting | Existing runtime success/error membership guards unchanged | Not a profile value | New property checks exact done rows, zero done-key writes and embedded/skipped counts; success/error guard-removal scratch mutants killed | artifact-bound |
| Seeded jitter / item order | No deployment change; labelled doubles only | Capacity is acknowledged before jitter/failure/reply; no hash ordering assumption | Not a profile value | Normal stage-flow twice and scratch reverse observation tasks; semantic paired-run checks retained | test-only |
| Test-only injected source / fault points | No runtime source write | One module copied, original __file__ retained for tool identity; all src hashes checked | Not a profile value | fix3-runtime-snapshot.json, red/reordering logs, final verification | test-only |

### Completion report - fix round 3

**Work completed (R-017, R-018, R-020):** all changes are tests/documentation. New tests/concurrency_fixtures.py supplies bounded, one-shot ArrivalGate. test_stage_flow.py uses it for fresh HTTP/model/S1/S2 capacity evidence before seeded jitter, failures or replies; targets depend on fixture pending work and configured cap, never item keys. Existing semantic/differential and failure/stop assertions are retained. The K1 native decode test uses a three-worker Barrier instead of a sleep. test_model_resources.py pins the gate's timeout and partial-tail behavior and uses arrival gates for configured capacity. K2-observe/K3/K4 edge doubles use the same gates and finally cleanup; K4 cancellation waits until both calls arrive. The lifecycle sampler acknowledges that it observed held capacity; model-composition asserts its existing arrival Event wait succeeded. The class table above lists every found concurrency count/peak assertion, its release determinant and why retained fake-clock/upper-bound/state/cleanup checks already are deterministic.

test_res1_resume.py adds test_k1_interrupted_done_text_is_immutable for both success and HTTP error, varying batch size 2-5, partial commit boundary and cap 1-3. It asserts exact done checkpoint rows/artifact bytes, no done-key commit/fail call, pending-only writes, exact embedded/skipped_done/failed counts, expected table membership and full replay batch context. The runtime text guards already implement the right behavior and were not edited.

**Result and evidence (CPU unit/mock_contract/property/replay; no live models):** ten distinct required/changed test files passed. Normal stage-flow ran twice; the full file also passed with scratch K2 observation tasks reversed. Across the twelve passing pytest processes: **875 passed, 35 skipped** (the ten distinct files once total 827 passed / 35 skipped; the two additional stage-flow runs contribute 48 passes). Three deliberate scratch-mutant failures are reported separately, never counted as passing tests. Exact absolute commands, PIDs, exits and summaries: .tmp/RES-1/fix3-started.jsonl and fix3-results.jsonl. Logs: fix3-<label>.log.

Command form from this worktree: `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q tests/<one-file>.py --tb=short --basetemp=<project>/.tmp/worktrees/RES-1/.tmp/RES-1/fix3-bt`. Environment: PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=this worktree's src. One pytest process at a time, no -n, CREATE_NO_WINDOW owned launchers; fixed basetemp removed after every completed run. Scratch checks add the test-only `-p fix3_inject` hook and evidence/tests import directories; exact arguments and labels are in the result ledger.

| File / run | Actual output |
|---|---|
| tests/test_stage_flow.py, normal first | 24 passed in 26.16s |
| tests/test_stage_flow.py, normal second | 24 passed in 28.60s |
| tests/test_stage_flow.py, scratch reversed observation task order | 24 passed in 28.99s |
| tests/test_res1_resume.py | 9 passed in 66.36s (0:01:06) |
| tests/test_k1_embed.py | 68 passed, 1 skipped in 17.99s |
| tests/test_model_resources.py | 24 passed in 0.91s |
| tests/test_k2_observe_edges.py | 20 passed in 6.24s |
| tests/test_k3_edges.py | 11 passed in 7.92s |
| tests/test_k4_aliases.py | 44 passed in 8.52s |
| tests/test_embed_server_lifecycle.py | 71 passed in 59.38s |
| tests/test_model_composition.py | 54 passed in 43.63s |
| tests/test_field_contracts.py | 502 passed, 34 skipped in 178.88s (0:02:58) |

**Scratch ordering check:** copy only k2_observe.py, replace its single `sorted(tasks.items())` with `sorted(tasks.items(), reverse=True)`, and inject it for that process while preserving the real __file__ for tool_revision. This changes actual task admission order without editing runtime disk bytes or changing the expected capacity. The whole stage-flow file passes, including failure/report order and serial/concurrent equivalence. No K2 naming ordered-draw semantics or production source are changed by this experiment.

**Red/green and mutants:** three non-equivalent scratch-only substitutions; each failed the intended assertion, not collection, timeout or crash. The new full generated text property ran against the scratch runtime mutants, with all its assertions intact, then passed on real runtime source. No live source was mutated and no test was weakened to kill a mutant.

| Mutant / selected test | Actual result |
|---|---|
| Remove text success membership guard; new text property [200] | killed, done-key commit spy assertion; 1 failed in 14.94s |
| Remove text error membership guard; new text property [500] | killed, done-key fail spy assertion; 1 failed in 12.18s |
| Force ArrivalGate open on first arrival; gate bounded/tail regression | killed, expected bounded AssertionError did not occur; 1 failed in 0.22s |

All three killed; no survivor. Logs preserve the exact source substitutions' effects and real assertion traces. fix3_checks.py's small mutation definitions are transcribed above; the iteration script itself is removed at handoff. Snapshot .tmp/RES-1/fix3-runtime-snapshot.json and final fix3-verification.txt record unchanged runtime identity throughout the completed checks. K1 remains SHA-256 `6649644d558aa4ebd73830f33728d1fd54aa0caac84fdba208e8a87365c8ea54`, matching fix round 2.

**Important changes/reasons:** use a one-shot first-arrival gate, not a repeated wave barrier: final partial waves and cached resumes must not wait for nonexistent work. Seeded jitter remains only to vary completion/failure order after capacity is acknowledged. A gate timeout is a test failure, not permission to release early. The thread sampler now proves it observed the cap before holders are expired. Runtime behavior was confirmed correct; no deployment decision or production source change was necessary.

**Configuration audit:** completed above. No runtime change, new key, schema, profile/example, default, model/provider, precision, resource limit, image policy, retry or deadline. All new bounds/targets are test-only synchronization controls. The unchanged field-contract suite and resource limit refusals/overrides passed; parser/image properties in test_k1_embed and interrupted-run properties passed.

**Checks / not_run:** final static source hashes, test syntax, complete PID/result ledger, fixed-basetemp/scratch absence and scoped git diff --check are recorded in fix3-verification.txt. Ruff check and format were attempted for all nine changed/new test/helper files, both exit 1; neither check ran because ruff is absent, and no install was attempted. Exact output from each command:

```text
<project>\.venv\Scripts\python.exe: No module named ruff
```

Full suite/test_kb_sync_flow.py/CI and independent retest are not_run here (PM-owned). Live model/GPU/download/install/external network work is prohibited and not_run; existing bounded loopback fixtures used only labelled engine/model doubles and closed with their tests. No Git writes or secrets access. A clock.sleep call was rejected by the role's exact tool allowlist; no escalation or permission change followed. Normal owned-exec output collection completed every authorized run.

**Changed files this round:** tests/concurrency_fixtures.py (new), tests/test_stage_flow.py, tests/test_res1_resume.py, tests/test_model_resources.py, tests/test_k2_observe_edges.py, tests/test_k3_edges.py, tests/test_k4_aliases.py, tests/test_embed_server_lifecycle.py, tests/test_model_composition.py; tasks/active/RES-1-resource-use.md. No src/config/contracts/profile file changed. All predecessor unstaged work remains untouched.

**Problems/risks/lessons:** no required implementation defect remains. Bounded tests still require OS scheduling within their stated timeout; they fail explicitly if that budget is exceeded. A sleep or a favorable source-hash permutation is not evidence of capacity: hold enough arrivals to prove it, then permit variable completion order. A semantic resume differential needs physical write/accounting assertions to protect completed work.

### Handoff - fix round 3

- **Current status: REVIEWING.** Test defects corrected and required verification complete, awaiting independent PM retest/integration.
- **Already done:** Step 0, full assertion/release sweep, test-only arrival gates and native/observation acknowledgments, new success/error text property, red/green scratch evidence, three mutants killed, all ten files green; stage-flow twice normally plus reversed task order. No-runtime/no-new-key audit complete.
- **Next PM action:** independent retest; reserved integration/full-suite/CI and Git delivery. Retain the explicit pending-work targets and bounded failure behavior when extending fixtures.
- **Do not repeat:** do not edit runtime to stabilize tests, use hash ordering or sleeps as capacity proof, release an unmet gate on timeout, or gate every later partial wave/resume with the original full cap.
- **Worktree/base:** same RES-1 worktree and inherited unstaged patch from dispatch; no Git state/history writes. This continuation changes tests/documentation only.
- **Lifecycle/cleanup:** all fifteen owned test/mutant limiter processes returned and were awaited; no backend/model service started. Bounded loopback fixtures ended with pytest. Fixed basetemp and scratch module removed after each use; owned launcher/import-hook/inventory/verification iteration sources removed at handoff. Retain only small logs, command/PID/result ledger, assertion inventory, runtime hash snapshot and final static report.
- **Noticed, not done:** no additional runtime defect; existing RES-2/native-operation residuals remain out of scope.
