# T-006 Storage layer

Kind: feature
R-IDs: R-014, R-016, R-017, R-015, R-009
Task file: tasks/active/T-006-storage-layer.md
Approach note: .tmp/T-006/research.md (the scout's research and hazard note S1-S3, copied into the worktree)

Starts once T-005 is accepted (decision 96). Even pipeline task: Claude (Opus 5.5, `high`) implements; Gemini 3.8 Flash reviews first and Codex reviews second, and the patch is never integrated on the Gemini pass alone (decision 91, family split). While the user is away a product-meaning question gets the PM's conservative, reversible choice, logged as a PM decision pending user review, unless it changes a requirement (decision 94).

## Brief header

- **Goal:** the storage layer every offline stage and the request path build on (`tasks/IMPLEMENTATION_PLAN.md:14`): typed Parquet and Arrow tables for the 16 tables of `contracts/README.md:28-45`, content-addressed artifacts keyed by input content hash plus model, prompt and schema revisions (R-017), checkpoints in `sync_state`, and immutable snapshots that are built, validated, published by moving `CURRENT` atomically, pinned by readers and rolled back (R-014, `contracts/README.md:135`, `docs/execution.md:33-39,66,70,74`). Every storage and lifecycle setting comes from the validated configuration of T-005, with the storage keys added to the read sets of the commands that use them; a missing value or a sentinel in a read key is refused at start (R-015). Nothing in this layer reads the source knowledge base, and with `rag.enabled: false` no store object is constructed (R-009, `docs/execution.md:78`).
- **Done check:** the new tests pass through `tools/agent-workflow/test_slot.py` on the project venv python, covering:
  - a round trip of every table on a tiny synthetic fixture, including Unicode and provenance fields, Arrow vectors with dimension and revision isolation, and an image shared by several folders;
  - refusal of a malformed schema, a duplicate key and a dangling reference;
  - an interruption after the data, after the manifest and before `CURRENT`: the old snapshot still serves, partial artifacts are redone, a checksum or size mismatch is refused, and a repeated resume is idempotent;
  - bounded concurrent readers, publishers and garbage collection: no corrupt `CURRENT`, pinned and trace-held snapshots survive, rollback to a collected target is refused, and no half-published snapshot is read;
  - property tests (Hypothesis 6.170.15 is installed) for identity normalisation, rename-invariant keys, invalidation and link-resolved path containment;
  - a non-default override test and a refusal test per storage setting;
  - a structural RAG-off test;
  - 3 to 10 mutants of the changed checks.
- **Scope:** the package from T-005 (`src/prompt_enhancer/`) with new storage modules and their tests, the T-005 read sets in `config.py` for storage keys only, `.tmp/T-006/` of the worktree, and this task file. A schema or contract change that S1 or S3 needs is proposed in Step 0 with its exact text and made only after approval. Read-only otherwise: `configs/`, `contracts/`, `docs/`, `tasks/REQUIREMENTS.md`.
- **Out of scope:**
  - K0 to K5 stage logic: scanning, hashing the source, embedding, observing, cards, aliases and index engines (T-007, T-008, T-010, T-011);
  - the serving gateway beyond the read and pin primitives;
  - network, GPU and cloud calls;
  - installs outside the project `.venv`; a missing package is a Step 0 question;
  - `.secrets/`, `database/` and `tools/agent-workflow/`.
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, a reply under 15 lines.
- **Step budget:** about 80 tool calls after Step 0 approval.
- **Run limit:** CPU only; no network, no GPU, no paid calls; every test bounded to seconds; concurrency tests use bounded threads or processes and temporary trees under the worktree `.tmp/`.
- **May assume / Must ask:**
  - May choose module, class and function names, the on-disk layout inside `storage.kb_store`, and the test layout.
  - Must ask (Step 0 questions) about:
    - the physical schemas (S1);
    - the publish, pin and GC coordination (S2);
    - new configuration keys and their sentinels (S3);
    - any new dependency;
    - any contract ambiguity.
- **Role pack and CLI wrapper:** implementer; Claude (profile `dev-large`, Opus 5.5 `high`).
- **Test sets:** not applicable: the pipeline suite under `tests/` is the one T-005 created; run it all plus the new tests.
- **Trivial:** no. **Small:** no.
- **Base:** commit 53535a3, which accepts T-005; working folder `.tmp/worktrees/T-006`.

### Research required before code

1. The scout's note `.tmp/T-006/research.md`: re-read every line it cites before relying on it (R-021), and work from the accepted T-005 code, not the unfinished code the scout read.
2. The table and protocol contract (`contracts/README.md:15-47,97,128-135`), `docs/execution.md`, `docs/model-storage.md`, and the stage documents of the callers (`docs/stages/K0-sync.md` to `K5-index.md`, `S0-intake.md`, `S1-candidates.md`, `S3-knowledge.md`, `S6-envelope.md`, `G-evaluation.md`).
3. The installed PyArrow 26.0.1 API for Parquet and Arrow IPC writes, atomic replace and `fsync`, read from the installed package source or its documentation, and quoted. Windows open-handle and replace behaviour is measured with a bounded test on this machine; Linux runs in CI (R-025).

### Hazards the Step 0 plan must resolve (from the scout, S1-S3)

- **S1:** the physical columns, types, nullability, manifest format and state enums are undefined (`contracts/README.md:28` lists main columns only). JSON artifacts (:97) must become columnar rows (R-016). Alias origin is "curated" in `K4-aliases.md:5` but "author" in `alias_set.v0.schema.json:36`. The card summary is consumed but not required.
  - Proposed: explicit versioned Arrow schemas; the contract conflicts go to the PM as proposed contract edits.
- **S2:** writer locks, crash durability, reader leases and trace references to attachments are undefined (`docs/execution.md:70,74`; `S6-envelope.md:20`).
  - Proposed: serialise metadata changes behind a single-writer lock, write manifests last, pin readers through leases, and let GC delete only what no snapshot, lease or trace references.
- **S3:** the units of `sync.snapshots.keep` and `sync.gc.retention`, copy and trace retention keys, and `sync.resume` versus `runtime.resume` precedence are undefined, and the T-005 read sets omit the storage keys.
  - Proposed: new validated keys with `design_required` sentinels until evidenced; the read sets declare each key actually read.

### Configuration impact

Proposed, pending PM approval of Q2 and Q3 below. The rows marked *new* or *schema change* depend on the contract edit in Q5. All keys are `configurable` unless noted. Every override and refusal test goes through `load_config` and then the storage factory, in `tests/test_storage_config.py`.

| Key | Schema (after Q5) | Example value | Read by (read set after this task) | Override test (non-default) | Refusal tests |
| --- | --- | --- | --- | --- | --- |
| `storage.kb_store` | string path; T-005 containment and link checks (`config.py:248-254`); the store resolves it again when it is built (TOCTOU) | `data/kb` (`configs/enhancer.example.yaml:318`) | all `kb *`; `enhance once`/`serve` and `eval run` with RAG on (`config.py:431-432`); `eval build` | the store is built at an alternate relative path, and every file it writes is under it | missing; sentinel; `../x`; a link that escapes the root; under `.secrets/`, `.git/`, `.venv/` or `models/` |
| `storage.data_root` | string path, as above | `data` (:317) | `enhance *`, `eval *` (already there); traces and eval tables live under it | traces and eval tables are written under an alternate root | missing; sentinel; outside the root |
| `storage.table_format` | **schema change:** enum `parquet`, `arrow` (today a free string, see the `show_schema` output) | `parquet` (:319) | the writers: `kb sync`, `kb rollback`, `kb gc` (registry rewrite), `enhance *`/`eval run` (traces), `eval build`/`eval run` (eval tables). Readers take the format from the manifest or the file suffix and do not read the key | `arrow` writes `.arrow` IPC files that read back identically | `csv`, sentinel, missing |
| `storage.lock.timeout_s` | **new:** number > 0 | `deploy_required` | every command that builds a store (the `kb *` commands; `enhance *`/`eval *` with RAG on; `eval build`) | a held lock and `timeout_s: 0.2` refuse within about 0.2 to 1 s; a larger value waits and succeeds | missing, sentinel, `0`, negative, string |
| `storage.lock.poll_s` | **new:** number > 0, at most `timeout_s` (checked at construction) | `deploy_required` | same as above | a non-default poll value still acquires the lock after release | missing, sentinel, `0`, larger than `timeout_s` |
| `sync.snapshots.keep` | integer ≥ 0 (existing); unit: **retired published snapshots kept besides `CURRENT`** | `design_required` (:39) | `kb sync` (via `sync`), `kb gc` (already there) | with `keep: 2` GC keeps two retired snapshots; with `0` it keeps only `CURRENT` plus the leased and held ones | sentinel; `-1`; missing |
| `sync.gc.retention` | **schema change:** string `^[0-9]+[smhd]$`; unit: time since GC first recorded the artifact as unreferenced | `design_required` (:40) | `kb sync`, `kb gc` (already there) | `0s` removes an orphan at the first GC; `1d` keeps it | `abc`, `5`, `-1d`, sentinel, missing |
| `sync.resume` | boolean (existing) | `true` (:41) | `kb sync` (via `sync`) | `false` redoes artifacts whose `sync_state` row is `done` | sentinel |
| `rag.enabled` | boolean (existing), read by the caller | `true` | request path | `false`: the knowledge store factory is never called; traces still go to `data_root` | (structural test, no store) |

Retained literals (no key): `CURRENT`, `MANIFEST.parquet`, `LOCK`, `WRITER`, the `leases/` and `holds/` names (`artifact-bound`); SHA-256 for checksums and artifact keys (`protocol-bound`, `contracts/README.md:16-17`); physical schema version `1` (`artifact-bound`); the PyArrow write options passed explicitly instead of left to defaults: Parquet `version='2.6'`, `compression='zstd'`, Arrow IPC uncompressed (`artifact-bound`, recorded in the manifest; see Q3); Hypothesis example counts and concurrency test sizes (`test-only`). `runtime.resume` is not read by this layer (Q3).

## Step 0

### Evidence gathered (R-021)

- **Accepted T-005 (`src/prompt_enhancer/config.py`, base 53535a3):** `COMMAND_READS` at :30-52; `kb status`/`rollback`/`audit`/`show` read only `storage.kb_store` (:31-38); `kb gc` reads `storage.kb_store`, `sync.gc.retention` and `sync.snapshots.keep` (:36); RAG on adds `storage.kb_store` to `enhance *` and `eval run` (:431-432). Path containment and link resolution: `_resolve_within` (:78-112) and `_configured_path` (:216-237), applied to `storage.data_root` and `storage.kb_store` at load (:249). The scout's line numbers came from the unfinished T-005 and are stale: for example it cites the example storage block at :314, but it is at `configs/enhancer.example.yaml:317-319`.
- **Schema (`enhancer_config.v0`, from the `show_schema.py` output):** `storage` = {`data_root`, `kb_store`, `table_format`}, all three of type `string`, all required, `additionalProperties: false`. `sync.gc.retention` is `type: string`; `sync.snapshots.keep` is an integer ≥ 0; `sync.resume` and `runtime.resume` are booleans.
- **PyArrow 26.0.1 (installed source):** `pq.write_table(table, where, ..., version='2.6', ..., compression='snappy', ...)` (`pyarrow/parquet/core.py:2017-2018`); `ipc.new_file(sink, schema, *, options=None, metadata=None)` (`pyarrow/ipc.py:191`). Neither has an fsync option (a grep of `io.pxi` for `fsync` finds nothing), so durability is the caller's job.
- **Probe on this machine, Windows 11, Python 3.12.10 (`.tmp/T-006/probe_fs.py`, run through `test_slot.py`, exit 0):**
  ```text
  1 parquet write to python file: closed_by_pyarrow=False      -> we can flush + os.fsync + close ourselves
  2 ipc write to python file: closed_by_pyarrow=False
  3 os.replace over target held open by python open(): PermissionError: [WinError 5] Access is denied
  3b os.replace after reader closed: ok
  5a unlink memory-mapped file: PermissionError: [WinError 32] ... being used by another process
  5b rmtree dir with mapped file: PermissionError: [WinError 32]
  5c rmtree after close: ok
  6 rmtree after pq.read_table returned (table still referenced): ok
  7 os.replace dir onto non-empty dir: PermissionError: [WinError 5]
  7b os.rename dir to new name: ok
  8 second O_EXCL create: FileExistsError
  9 msvcrt second LK_NBLCK on same byte: PermissionError  (two handles in one process conflict)
  9b msvcrt LK_NBLCK after release: ok
  10 os.open(dir) for fsync: PermissionError               -> no directory fsync on Windows
  ```
- **Normalisation probe (`.tmp/T-006/probe_norm.py`, Unicode 15.0.0):** `casefold(NFKC(c))` is idempotent for every single code point (0 failures in 0x110000). Sequences are left to the Hypothesis property test, and a counterexample would be reported, not patched (the contract allows only NFKC and case folding, `contracts/README.md:22`).
- **Dependency:** `pyarrow` is installed in the venv (scout's `pip list`) but `pyproject.toml:8` declares only `PyYAML` and `jsonschema`.
- **Recall:** `recall.py build` gave `indexed=84 ... chunks=418`. Searches for snapshot, retention, resume and alias-origin found no earlier decision on these points (only decision 19 and N125).

### Unknowns and failure modes

1. Physical types, nullability, the manifest format and state enums are undefined (S1).
2. On Windows a reader's open handle blocks `os.replace` of `CURRENT` and deletion of a snapshot (probe 3, 5a, 5b), so readers and writers must be coordinated, not left to retry. A directory cannot be published by renaming it over a non-empty one (probe 7).
3. A crash can leave `.tmp` files, a snapshot directory still being built, an unsealed manifest, or a lease that no process owns any more.
4. GC can race a sync that has written artifacts no manifest lists yet, a pin that has read `CURRENT` but not yet written its lease, or a rollback.
5. Retention units, the lock timeout and resume precedence are not defined (S3). A table read whose checksum is verified on every request could cost too much at unknown scale (decision 71).

### Questions (ranked, each with a proposed answer)

**Q1 (S1, physical schemas): how are the 16 tables typed?**
Proposed answer:
- Schemas are defined in code (`storage/tables.py`). Each is a `pyarrow.schema` tagged with metadata `pe.table=<name>`, `pe.physical_version=1`.
- Every column the contract lists (`contracts/README.md:30-45`) gets an explicit type and nullability. Strings are `large_string`; ids are non-null; times are `timestamp('us', tz='UTC')`; counts are `int64`; vectors are `fixed_size_list<float32, dim>`.
- Each embedder revision is a separate Arrow IPC file, so two dimensions never share a column.
- Enum columns are checked against the enum in the contract's own JSON schema where one exists (for example alias `origin` is read from `alias_set.v0`), so no list is duplicated in code.
- The nested JSON artifacts (`concept_card.v0` facets, variant groups, statements, lint and revisions; the `eval_scenario.v0` bodies; per-call records; trace bodies) are one column per top-level field. Scalar fields are typed; object or array fields are a `large_string` of canonical JSON, validated against the subschema on write and on read. This satisfies "a table row set" (:97) and R-016 without fixing a nested Arrow layout to a draft v0 schema.
- The manifest is itself a Parquet table, `MANIFEST.parquet`, with columns `path`, `table`, `format`, `size_bytes`, `sha256`, `rows`, `physical_version` and `artifact_key` (for referenced artifacts). Its SHA-256 is written into `CURRENT` and the `snapshots` row.
- State enums:
  - `sync_state.state`: `queued` (K2 first-use queue, `K2-observe.md:12`), `done`, `failed` (undecodable input, `docs/execution.md:37`).
  - `snapshots.state`: `building`, `validated`, `published`, `retired`, `collected`, `failed`.
- Contract conflicts: the storage layer follows whatever the schema file says, so no code changes when a doc is fixed. Q5 proposes the doc fixes. `cards.summary` stays nullable, as in v0. That K1 and K4 need it is a K3 (T-010) schema question, listed under Noticed.

**Q2 (S2, coordination): how are publish, pin and GC coordinated?**
Proposed answer:
- **Store lock.** `kb_store/LOCK` holds an OS byte-range lock (`msvcrt.locking` `LK_NBLCK` on Windows, `fcntl.flock` on POSIX), polled until `storage.lock.timeout_s`. Each critical section is short and does one of four things:
  - publish or rollback (rewrite the registry, then `os.replace` of `CURRENT`);
  - pin (read `CURRENT`, check its size and manifest hash, create a lease);
  - GC (decide and delete);
  - registry writes.
  Because every reader reads `CURRENT` only under the lock, our own handles never block the replace (probe 3). An outside handle (antivirus, indexer) makes the replace fail atomically: the old `CURRENT` stays and the publish is reported as failed.
- **Writer lock.** `kb_store/WRITER` is held for a whole `kb sync` and for `kb gc` and `kb rollback`, so a second sync, or a GC during a sync, is refused at once (R-014 single writer, and the race in failure mode 4).
- **Leases.** One file `leases/<uuid>` per pin, kept open with an OS lock by the reader process until it is released. GC counts a lease as live when it cannot lock the file, so a lease whose process crashed becomes stale and is removed without PID checks.
- **Trace holds.** `holds/<request_id>` rows record (`snapshot_id`, `expires_at`). The S6 writer (T-015) computes the expiry from its own trace retention key, so this layer adds no trace key (Q3).
- **Snapshot build:**
  1. Write the files into `snapshots/.building-<id>/`.
  2. For each file: `*.tmp`, then flush, `os.fsync`, close, then `os.replace` to the final name.
  3. Write the manifest last.
  4. Validate: schema, unique keys, references, checksums.
  5. Rename the directory to `snapshots/<id>/` (probe 7b).
  6. Under the lock: write the `snapshots` row, then replace `CURRENT`.
  On POSIX the parent directory is also fsynced. On Windows that is impossible (probe 10), and the assumption that NTFS journals renames is labelled as an assumption.
- **Verification:**
  - At pin: size and manifest hash.
  - At validate and rollback: every file's SHA-256.
  - In a reader process: a file's SHA-256 the first time it is opened (cached by path, size and mtime).
  - A mismatch refuses the operation with `StorageError`.
- **GC never removes:**
  - `CURRENT`;
  - the `keep` newest retired snapshots;
  - a leased snapshot;
  - a held snapshot;
  - an artifact listed by any remaining manifest, or unreferenced for less than `sync.gc.retention`.
  A `PermissionError` while deleting (an outside handle, probe 5a) skips that item, reports it and leaves it `retired`. A snapshot becomes `collected` only after it is fully removed.
- **Rollback** targets only a `retired` snapshot whose directory and checksums are intact. A `collected` target is refused.
- *Dropped alternative:* lock-free reads of `CURRENT` with retry on `PermissionError`. A pin that reads an old pointer could race GC between reading `CURRENT` and creating its lease, and retry counts would be another guessed number.

**Q3 (S3, settings): which keys are added and how are units and precedence set?**
Proposed answer:
- **Add** `storage.lock: {timeout_s, poll_s}` with example `deploy_required`. The wait depends on the machine's disk and the request concurrency. The concurrency tests will report the measured lock hold times as evidence for a later value.
- **Keep** `sync.snapshots.keep` (count of retired snapshots) and `sync.gc.retention` (duration string `<n>s|m|h|d`, measured from the first GC that saw the artifact unreferenced, recorded in `sync_state` as stage `gc`). Both stay `design_required`.
- **Restrict** `storage.table_format` to `parquet` or `arrow`.
- **Add no attach-copy key and no trace-retention key.** No code in this task reads them: image copies are written by K0/K5 (T-007, T-011) through a generic `put_blob`, and trace retention belongs to T-015.
- **Resume precedence:** `kb *` commands read only `sync.resume`; `runtime.resume` belongs to the request and eval runtime. No command reads both, so there is no precedence to define. The checkpoint API takes `resume` from the factory that read `sync.resume`.
- **PyArrow write options** (Parquet version, compression) are explicit `artifact-bound` literals recorded per file in the manifest, not keys: they change bytes, not meaning, and readers take them from the file. *Alternative:* `storage.parquet.compression` as a key, if the PM prefers.
- **Read-set additions in `config.py`:**
  - `storage.lock`: every `kb *` command; `eval build`; with RAG on, `enhance *` and `eval run`.
  - `storage.table_format`: `kb sync`, `kb rollback`, `kb gc`, `enhance *`, `eval build`, `eval run`.
  - `storage.data_root`: `eval build`.

**Q4 (dependency): may the existing PyArrow be declared?**
Proposed answer: add `"pyarrow>=26.0.1"` to `pyproject.toml` `dependencies`. No install: it is already in the venv. This touches `pyproject.toml`, which is outside the listed Scope, so it needs approval.

**Q5 (contract edits, exact text, made only after approval).** Proposed answer: approve these edits.

1. **`contracts/schemas/enhancer_config.v0.schema.json`, under `storage.properties`:**
   - `table_format` becomes `{"anyOf":[{"enum":["parquet","arrow"]},{"enum":[<sentinels>]}]}`.
   - Add a `lock` object `{"timeout_s":{"anyOf":[{"type":"number","exclusiveMinimum":0},<sentinels>]},"poll_s":<same>}` with both fields required and `additionalProperties: false`.
   - Add `lock` to `storage.required`.
   - `sync.gc.retention` becomes `{"anyOf":[{"type":"string","pattern":"^[0-9]+[smhd]$"},<sentinels>]}`.
2. **`configs/enhancer.example.yaml:319`:** append `  lock: {timeout_s: deploy_required, poll_s: deploy_required}   # store lock wait, set per machine (T-006)`. Comment the units at :39-40: `# retired snapshots kept besides CURRENT` and `# e.g. 7d: age since first seen unreferenced`.
3. **`contracts/README.md`, the Commit protocol paragraph:** add "Publish, rollback, pin and garbage collection serialise on one store lock (`storage.lock`); a request holds a lease on its pinned snapshot until the response is sent, and a trace may hold a snapshot until its retention ends; `kb gc` removes neither. `sync_state.state` is `queued`, `done` or `failed`; `snapshots.state` is `building`, `validated`, `published`, `retired`, `collected` or `failed`."
4. **`docs/stages/K4-aliases.md:5`:** change "origin (`curated` for the admin's" to "origin (`author` for the admin's", matching `alias_set.v0.schema.json:36`. *Alternative:* change the schema to `curated`, which needs a v1 schema.
5. **`docs/model-storage.md:13-24`:** move `registry/` (`sync_state`, `snapshots`) inside `kb_store/`, so the `kb *` commands read only `storage.kb_store` as their read sets already say, and show `kb_store` as the separately configured path (`data/kb`).

### Plan (finish checks map to the done check)

1. **Config and contracts (after Q3 to Q5).** Make the approved edits and the read-set additions in `config.py`.
   *Finish:* `test_storage_config.py` has an override and a refusal test per row of Configuration impact (done-check item 6), and the T-005 suite stays green.
2. **`storage/identity.py` and `storage/keys.py`.** Name normalisation, `concept_id`, `image_id`, folder-id checks, the artifact key (canonical JSON of kind, input hash, model/prompt/schema revisions and physical version) and link-resolved containment.
   *Finish:* Hypothesis properties: idempotent normalisation; a rename keeps the key; any change of revision or input changes the key; no resolved path leaves `kb_store` (item 5).
3. **`storage/tables.py`.** The 16 physical schemas, the validators (types, enums from the contract schemas, JSON-column subschemas, unique keys, references) and atomic durable write and read.
   *Finish:* a round trip of every table on a tiny Unicode fixture with provenance, vectors split by revision and dimension, and one image in two folders; malformed schema, duplicate key and dangling reference are refused (items 1-2).
4. **`storage/store.py`.** Layout, the lock and writer lock, artifacts, `sync_state` checkpoints, the snapshot builder, validate, publish, pin and lease, holds, rollback and GC, plus a knowledge-store factory that is never called with RAG off.
   *Finish:*
   - crash tests that abandon a build after the data, after the manifest and before `CURRENT`: the old snapshot still serves, `.tmp` and unlisted partial artifacts are redone, a corrupted size or hash is refused, and a second resume is idempotent;
   - concurrency tests (bounded `subprocess` children with `CREATE_NO_WINDOW` and `-X faulthandler`, plus threads, timeouts in seconds): readers versus publishers versus GC, a killed lease holder, a held snapshot, rollback to a collected snapshot;
   - the structural RAG-off test with `kb_store` absent and file access under it recorded as none (items 3, 4, 7).
5. **Finish.**
   - The full suite through `test_slot.py` (`-n` at most 2).
   - 3 to 10 mutants on a scratch copy (lease liveness, retention comparison, duplicate-key check, manifest hash check, keep count, lock timeout).
   - `ruff check` and `ruff format --check` output quoted.
   - The Configuration audit, the Completion report, and removal of the scratch files (item 8).

## Steps

- Step 1, done: made the approved contract and config edits (schema `storage.table_format` enum, `storage.lock`, the `sync.gc.retention` pattern; the example and overlay set `storage.lock` to `deploy_required`; README commit-protocol text; K4 `author`; model-storage layout; `pyarrow==26.0.1`) and the read-set additions in `config.py`.
  - The T-005 tests used `kb status` on the raw example, which now reads `storage.lock` and refuses its sentinel. Their config fixtures set a labelled test-only lock value. `test_real_loader_examples` now asserts that refusal and loads the examples with `profile check`. `test_field_contracts` probes `'arrow'` and `'7d'`. Result: 806 passed, 25 skipped, as before.
- Step 2, done: `storage/identity.py` and `storage/_fs.py`: identities, artifact key, durable atomic writes, link-resolved containment, OS file lock.
  - The Hypothesis test found that `inside(root, 'a/c:')` leaves the root on Windows (ValueError from a drive-like segment). Fixed: `:` is refused in every segment.
- Step 3, done: `storage/tables.py`: the 16 physical schemas.
  - Two portability fixes, both found by the round-trip test: timestamps use the fixed offset `+00:00`, because the zone name `UTC` needs `tzdata` on Windows to convert back; list items are named `element`, Parquet's compliant name, so both formats give identical schemas.
  - Cross-file `$ref` (eval_scenario to constraint_ledger) resolves through a `referencing` registry of every contract schema.
- Step 4, done: `storage/store.py`: knowledge store, writer session, checkpoints, snapshot builder, publish, pin and lease, holds, rollback, GC, data store, and the factories that read only declared keys.
- Step 5, done: full suite, 10 mutants (1 survivor, killed after one added assertion), cleanup. Ruff is not installed (`not_run`).

## Completion report

**Behaviour.** `prompt_enhancer.storage` provides the following.
- **Tables:**
  - Typed, versioned Arrow schemas for all 16 contract tables. Parquet or Arrow per `storage.table_format`; embeddings are always Arrow, one embedder revision and one dimension per file.
  - Writes check, and reads check again: the schema, nulls, enums (read from the contract schema files where they define them), JSON columns against their subschemas, whole documents (`concept_card.v0`, `eval_scenario.v0`), unique keys, identity rules (`concept_id` derived from the normalised name, the `image_id` and `folder_id` forms, the facet prefix) and finite vectors. Dangling references are refused at snapshot validation.
- **Artifacts and checkpoints:**
  - Artifacts are content-addressed: the key is the input hash plus model, prompt and schema revisions and the physical version, never a path.
  - `sync_state` checkpoints commit the artifact first and the row last, in append-only segments compacted when a writer session ends.
  - With `sync.resume: false`, only this session's rows count as done.
- **Snapshots and coordination:**
  - A snapshot is built in `.building-<id>`: data, then `MANIFEST.parquet` last, then full validation, then a rename to a new name.
  - Publish replaces `CURRENT` (`<id> <manifest sha256>`) under the store lock, then updates the registry. CURRENT is the truth, and recovery reconciles the registry with it.
  - `kb sync`, `kb gc` and `kb rollback` share one writer lock.
  - A reader pins under the store lock and holds a lease file through an OS lock. A lease whose process died is detected and removed.
  - A trace hold keeps a snapshot until its expiry. Rollback accepts only an intact retained snapshot.
- **GC** keeps CURRENT, the `keep` newest retired snapshots, and every leased or held snapshot. It removes an orphan artifact once it has been unreferenced for `sync.gc.retention`, and drops that artifact's done row.
- **RAG off:** with `rag.enabled: false`, `open_knowledge_store` refuses for the request path, so no store object exists; traces go to the data store under `storage.data_root`.

**Commands and results** (every command through `test_slot.py`, venv python, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`):
- `pytest -q -s -p no:cacheprovider --basetemp=.tmp/T-006/pt tests` gave `864 passed, 25 skipped in 180.53s`. That is the 806 T-005 tests plus 58 storage tests; parametrised cases are counted separately. The size-check assertion was added later, and `tests/test_storage_snapshots.py` alone then gave `17 passed in 5.53s`.
- Concurrency evidence for a later `storage.lock` value. These are wall times of `pin()` and `publish()` on this Windows machine, an upper bound of the store-lock hold, from 2 reader processes (25 pins each), 1 publisher (4 snapshots) and 1 GC process (4 runs): `{"reader": {"n": 50, "median_s": 0.0103, "max_s": 0.1067}, "publisher": {"n": 4, "median_s": 0.0289, "max_s": 0.0319}}`. This tiny fixture shows only that the lock works; it is not a deployment figure.
- Mutants (`.tmp/T-006/mutants.py` on a scratch copy of `src/`): 10 run, 9 killed at first:
  - killed: drive-segment containment, lock timeout, lease liveness, retention boundary, keep count, manifest hash check, `sync.resume: false`, duplicate key, dangling reference;
  - survived: the size check in `_verify`, because the SHA check behind it masked it. After an added assertion (a file that grows after the pin is refused by size) the rerun gave `('size check', 'killed', ['1 failed, 7 passed in 2.57s'])`.
- Windows probe and normalisation probe: see Step 0 (`.tmp/T-006/probe_fs.py`, `.tmp/T-006/probe_norm.py`).

**not_run:**
- The POSIX path, `fcntl.flock` and the directory `fsync`: Linux runs only in CI (R-025), so it is not claimed here.
- `ruff check` and `ruff format --check`: ruff is not installed in the venv or on the PATH (`which: no ruff`), and installing it is outside the brief.
- The symlink escape inside the containment property test: its case is skipped when symlinks cannot be created. Whether this machine created the link was not recorded.
- `pytest -n`: xdist is not installed (`unrecognized arguments: -n`), so the suite ran serially.

**Assumptions:**
- On Windows a directory cannot be fsynced (probe 10), so the durability of a completed `os.replace` or `os.rename` relies on NTFS journaling its metadata. This is written in the `fsync_dir` comment and is not measured.
- A file hashed once per process is trusted again while its path, size and mtime are unchanged. A same-size rewrite that also restores the mtime would go undetected until the next process. This is the cost tradeoff from Q2.

**Configuration audit:** every row of `Configuration impact` is implemented with its tests in `tests/test_storage_config.py`. Each row has a non-default override and refusals for missing, sentinel and invalid values, through `load_config`, and also through the store factory for a dict that bypassed the loader.
- `kb_store`: `data/alternative-kb`; refused: `../outside`, `.secrets/kb`, `models/kb`, sentinel.
- `data_root`: `data/alternative-data`; refused: `../elsewhere`, sentinel.
- `table_format`: `arrow`, which writes the registry and table files as `.arrow`; refused: `csv`, `Parquet`, sentinel. An undeclared key is refused at use.
- `lock.timeout_s`: `0.2` times out within 0.15 to 2 s; refused: `0`, `-1`, `soon`, `null`, missing, sentinel.
- `lock.poll_s`: `0.5` acquires after release; refused: `0`, sentinel, and `poll_s > timeout_s`.
- `snapshots.keep`: `2` keeps two retired snapshots; refused: `-1`, `1.5`, sentinel.
- `gc.retention`: `1d` gives 86400 s; refused: `abc`, `5`, `-1d`, `1w`, sentinel, and `1d\n`. The schema pattern's `$` accepts a final newline, so the store's `fullmatch` is the strict check.
- `sync.resume`: `false` redoes done rows; refused: sentinel, `yes`, and undeclared for `kb gc`.
- `rag.enabled: false`: the structural test, with `kb_store` absent and an audit hook recording no access under it.

Retained literals and their classification are as listed under Configuration impact. One addition: `HASH_CHUNK` (1 MiB streaming read) is `safety-bound`. Child-process timeouts and Hypothesis example counts are `test-only`.

**Changed files:**
- Modified:
  - `contracts/schemas/enhancer_config.v0.schema.json`, `contracts/README.md`, `docs/model-storage.md`, `docs/stages/K4-aliases.md`
  - `configs/enhancer.example.yaml`, `configs/overlays/deploy-vllm.example.yaml`
  - `pyproject.toml`, `src/prompt_enhancer/config.py`
  - `tests/test_field_contracts.py`, `tests/test_fix01.py`, `tests/test_fix02.py`, `tests/test_fix03.py`, `tests/test_loader.py`, `tests/test_real_contracts.py`
  - this task file
- New:
  - `src/prompt_enhancer/storage/{__init__,_fs,identity,tables,store}.py`
  - `tests/storage_fixtures.py`, `tests/storage_child.py`
  - `tests/test_storage_{tables,snapshots,concurrency,properties,config}.py`
- `configs/enhancer.dev.example.yaml` is unchanged: it extends the full example and inherits `storage.lock`.

**Research evidence:** Step 0 (PyArrow source lines, Windows probes, normalisation probe). PyArrow keeps Python file objects open after writing (probes 1 and 2). `pq.read_table` with `memory_map=False` leaves no handle that blocks a delete (probe 6).

**Open issues for review:**
- Leases and holds are files the request path writes under `kb_store`. They are coordination metadata, not knowledge data (`kb sync` stays the only writer of snapshots, R-014), but a reviewer should confirm that reading of R-014.
- GC deletes snapshot directories outside the store lock while holding the writer lock. This is safe because only CURRENT gains leases and only a retained snapshot can be rolled back to.

## Fix round 1

**Root cause** (the PM's reading, confirmed in the code): the store trusted three things at the moment it acted, without checking them again.
- **Paths:** a target built from a caller argument or a store subdirectory was used without resolving links (F1, F2).
- **State:** a decision (delete this snapshot, this helper may write) was made once and acted on later, outside the lock or session that made it (F3, F7, G4).
- **Evidence:** what a snapshot or checkpoint claimed was not verified (F4, F6, F8, F10), and the configuration and the registry format change assumed the happy path (F5, F9).

**Changes:**
- **One containment helper.** `KnowledgeStore.path(relative)` goes through `_fs.inside`, which resolves links component by component and refuses anything outside the store root.
  - Every read, write, rename and delete in `store.py` takes its path from it: `LOCK`, `WRITER`, `CURRENT`, the registry and its pointer, `sync_state` segments, artifacts, leases, holds, snapshot and build directories, directory listings (`_children`), and the GC walk (each file re-resolved).
  - `DataStore` uses `inside(data_root, ...)` the same way.
  - `release_hold` validates the request id, as `hold` does (F1, F2).
- **One ownership guard.** `Writer.require()` is the first call of every method of `Checkpoints` (`commit`, `queue`, `fail`, `done`, `load`, `state`), of `SnapshotBuilder` (`write_table`, `add_file`, `reference_artifacts`, `write_manifest`, `finish`), and of `Writer` (`checkpoints`, `begin_snapshot`, `publish`, `rollback`, `gc`, and the `sync_state` append). A helper that outlives its writer is refused (F7).
- **GC acts under the lock that decides.**
  - In one store-lock section GC reads leases and holds, chooses the candidates and marks them `deleting` (a new `snapshots.state`).
  - `hold()` accepts only `published` or `retired`, and rollback only `retired`. So a hold that arrives after the decision is refused, and one admitted before it protects the snapshot (F3).
  - A delete that fails stays `deleting` and is retried by the next GC.
  - Validated snapshots this writer session built and has not yet published are skipped (`GcReport.session`), and so are artifacts referenced by an unfinished build of this session (G4).
- **Evidence is verified before it is trusted.**
  - `reference_artifacts` records each artifact's size and SHA-256 in the manifest, and every manifest entry must have both. `finish` and `rollback` verify artifacts fully; `publish` checks that each exists and has its recorded size (F4).
  - A pin still skips artifacts, because readers never open them.
  - Full validation calls the new `check_embedding_space`: one embedder revision and one dimension across both vector tables (F6, `K1-embed.md:9`).
  - `Checkpoints.done()` verifies size and SHA-256, so corrupt output is redone, not skipped (F8).
  - `check_tags` requires both `pe.table` and `pe.physical_version` on every table, the manifest and hold files (`HOLD_SCHEMA` is now tagged) (F10).
- **Registry format change recovers from a crash at any step.** The registry is written to a uniquely named `registry/snapshots-<n>.<fmt>`. Then `registry/REGISTRY` is atomically replaced to name it, then the other files are swept. Readers trust only the file the pointer names, and each writer session sweeps the leftovers (F5).
- **Finite lock values.** The store factory and the `KnowledgeStore` constructor refuse non-finite, non-positive and boolean values. The schema adds `"maximum": 1.7976931348623157e+308` to both `storage.lock` fields, so the loader refuses `.inf` at start (F9).
  - That literal is `protocol-bound`: the largest finite IEEE-754 double.
  - JSON Schema cannot refuse NaN, because every comparison with NaN is false; the store refuses it, and a test documents this.
- **Docs.**
  - `contracts/README.md`: the `concept_text_embeddings` key now includes `text_hash` (G1); the `deleting` state and the finite-lock rule are in the commit-protocol paragraph.
  - `docs/model-storage.md`: the layout shows `REGISTRY`, `leases/` and `holds/`, and says that `LOCK`, `leases/` and `holds/` must stay writable by the serving runtime's account while snapshots, `registry/` and `artifacts/` are written only by the `kb` writer commands (G3).

**Before and after.** The Codex probes were rerun as `.tmp/T-006/review-r1/rerun_probes.py`: same inputs as `probe_review.py` and `probe_dependencies.py`, writing under `.tmp/T-006/` and recording a refusal per section. Outputs are in `before.jsonl` and `after.jsonl`.

| Finding | Before | After |
| --- | --- | --- |
| F1 | `accepted`, victim deleted | `refused: invalid request id`, victim exists |
| F2 direct | `wrote_outside=True` | refused (resolved outside), outside empty |
| F2 factory | `accepted`, file outside | refused, outside empty |
| F3 | hold accepted, snapshot deleted | `hold_refused: cannot be held: deleting`, `held_but_deleted: false` |
| F4 | `published` | refused (artifact missing) |
| F5 | `two snapshot registries exist` | `accepted, states=failed,published` |
| F6 | `published` | `refused: snapshot mixes embedding spaces` |
| F7 | queue and add_file `written` | both `refused: ... a helper outlived its writer` |
| F8 | `skipped: true` | `skipped: false` |
| F9 | `accepted timeout_s=inf` | `refused: storage.lock.timeout_s: inf is greater than the maximum of 1.7976931348623157e+308` |
| F10 | `accepted` | `refused: folders: file is tagged as table None` |

**Tests** (new `tests/test_storage_review_r1.py`; also `tests/test_storage_config.py`, which now asserts that the `REGISTRY` pointer names a `.arrow` file):
- **F1:** four request ids built from paths are refused.
- **F2:** the class test is parametrised over every store write directory (`artifacts`, `registry/sync_state`, `registry`, `snapshots`, `leases`, `holds`), each with a directory link leaving the store and the operation that writes there, plus the `DataStore` traces directory. The link is a junction on Windows, which needs no privilege, and a symlink on Linux. These ran here, not skipped.
- **F3:** a late hold is refused and the snapshot is collected; an early hold protects; a failed delete stays `deleting` and is retried.
- **G4:** this session's unpublished snapshot survives GC; a later session collects an orphan.
- **F4:** artifact deleted, rewritten at the same size, or rewritten at a new size before `finish`, plus deleted before `publish`.
- **F6:** a mixed revision and a mixed dimension.
- **F7:** the class test calls all 16 helper methods after their writer closed, while another writer owns `WRITER`.
- **F8:** same-size corruption is redone.
- **F10:** missing table and version tags, an untagged manifest, a tagged hold.
- **F5:** a Hypothesis property test (40 examples) crashes the registry write at step 1 to 5 for every pair of formats. It checks that recovery succeeds, that the registry holds either the old or the new rows, that exactly one registry file remains and is the one the pointer names, and that a later publish works.
- **F9:** the loader refuses `inf`, `-inf`, `0` and `-1` for both keys; the store refuses `nan`, `inf`, `0`, `-0.5` and `True` for both; a NaN that passes the schema is refused by the store.

**Results** (through `test_slot.py`, `PYTHONPATH=src`):
- full suite `pytest -q -s -p no:cacheprovider --basetemp=.tmp/T-006/pt tests`: `906 passed, 25 skipped in 182.66s`; the 25 skips are the same T-005 skips as before;
- lock hold times in this run: `{"reader": {"n": 50, "median_s": 0.0065, "max_s": 0.1969}, "publisher": {"n": 4, "median_s": 0.0624, "max_s": 0.0645}}`. Publish is now slower than in round 0 because it also checks the referenced artifacts and writes the registry pointer.

**Mutants** (`.tmp/T-006/review-r1/mutants_r1.py`, scratch copy of `src/`): 14 run, 14 killed.
- `store.py` (10): F1 id check, F2 containment, F7 session guard, F3 hold admission, F3 `deleting` mark, G4 session protection, F4 publish artifact check, F8 checksum in `done`, F5 registry sweep, F9 finiteness.
- `tables.py` (4): F10 table tag, F10 version tag, F6 dimension check, F6 revision check.

**Configuration audit delta:**
- `storage.lock.timeout_s` and `storage.lock.poll_s` are now finite and positive. The schema `maximum` is `protocol-bound` (largest finite double), and the store also refuses NaN and booleans.
- The examples are unchanged: `deploy_required` stays.
- New retained literals, all `artifact-bound`: `REGISTRY_POINTER` (`registry/REGISTRY`), the registry file name pattern, the hold table tags (`pe.table: hold`), and `HOLDABLE` (`published`, `retired`). The last is protocol-bound to the contract states.
- No new key.

**not_run:**
- The Linux path: `fcntl`, directory fsync, and the symlink branch of the link tests; it runs in CI only.
- Ruff, which is not installed.
- `probe_writer_death.py`: Codex already reported it passing, and this round did not change the writer-death behaviour.

**For the T-008 owner:**
- The `concept_text_embeddings` key now includes `text_hash` (contracts/README.md), so several aliases of one concept can each have a vector. Please confirm this, or change the contract.
- A snapshot now refuses vectors from two embedder revisions or two dimensions, so K1 must embed images and concept text with one revision per snapshot.

**Residual:** a link created inside the store between the containment check and the write (TOCTOU) is not prevented. The store directory belongs to the writer, and every later operation checks again.

## Fix round 2

Step 0 (small fix round inside a started task, binding brief from the PM): reproduce R1-R3 → put each check where the store acts on the fact → class tests, mutants, full suite. No open questions: the brief fixes the approach (one `verify_snapshot`, one safe walker, one session per Writer).

**Root cause** (the PM's reading, confirmed in the code). Round 1 placed each check at one call site, not at the point where the store trusts the fact.
- **Integrity (R2):** `finish` verified everything, but `_move_current` checked artifacts by size only (`store.py:592` before this round). `commit()` could still rewrite a referenced artifact after `finish`, and a same-size rewrite that kept the mtime also passed the per-process hash cache.
- **Containment (R1):** `_artifact_files` used `os.walk`. That checked each file but not each directory, and on Windows it descended into a junction, which `followlinks=False` does not cover (`store.py:683` before this round).
- **Session (R3):** `require()` asked only whether the Writer's lock was held. A closed Writer could be entered again: its old helpers came back to life, and it kept its run id (`store.py:495` before this round).

**Changes:**
- **Acting points verify again** (`store.py`):
  - `KnowledgeStore.verify_snapshot(sid, sha)` checks every snapshot file and every referenced artifact by size and a fresh SHA-256, then the tags and schemas, completeness, references and the embedding space.
  - `_move_current`, the one place `CURRENT` moves, calls it under the store lock right before the replace. So publish and rollback both use it, and rollback no longer verifies twice.
  - `_verify(..., fresh=True)` bypasses the per-process hash cache. Publish, rollback, `finish` and `Checkpoints.done` (resume) use it; reader pins keep the cache.
  - `_manifest` parses the same bytes it hashed.
- **Referenced artifacts are immutable:**
  - `Writer._referenced()` maps each artifact path to the SHA-256 values recorded for it by every `validated`, `published` or `retired` snapshot (`LIVE`) and by this session's unfinished builds. It is cached, and the cache is reset by `reference_artifacts` and after GC collects snapshots.
  - `Checkpoints.commit` passes a `check` to `atomic_write_bytes` and `write_table_file` (new optional parameter in `_fs.py`). It refuses content whose SHA-256 is not a recorded value before the file is replaced. Writing the recorded bytes again, to repair a damaged file, is allowed.
  - GC uses the same map.
- **One safe traversal:**
  - `_fs.real_inside(root, rel)` is `inside` plus "no link anywhere": the resolved path must equal the lexical one.
  - `KnowledgeStore.real`, `_children` and `walk` check each directory with it before listing it, and each entry after.
  - `_remove_tree` walks first, then calls `shutil.rmtree`. Recovery of `.building-*` and GC snapshot removal use it.
  - GC lists `artifacts/` before it changes anything, so a link refuses GC before any `deleting` mark.
  - A link found inside a snapshot that GC is deleting skips that snapshot. It stays `deleting`, is reported in `skipped` and is retried by the next GC.
  - `DataStore.read` lists through `real_inside` too.
  - Every listing in the store now goes through `_children`: leases, holds, the registry sweep, `sync_state` segments, snapshots, recovery and GC.
- **One session per Writer:**
  - A Writer is single-use: a second `__enter__` raises. Its `run_id` is created when the session opens.
  - Every helper already calls `Writer.require()`, so a helper can act only inside the session it was made in.
  - No caller re-entered a Writer (`tests/storage_child.py` and every test open a new one).

**Before and after** (`.tmp/T-006/review-r2/probe_r2.py`, the Codex reproductions; outputs in `before.jsonl` and `after.jsonl`):

| Finding | Before | After |
| --- | --- | --- |
| R2 | `overwrite: accepted`, `publish: published`, `artifact_matches_manifest: false` | `overwrite: refused: ... referenced by a snapshot, so its bytes cannot change`; the intact snapshot publishes, `artifact_matches_manifest: true` |
| R1, empty outside folder | `gc: accepted`, `outside_scandir: 1` | `gc: refused: ... artifacts/linked: resolved outside project root`, `outside_scandir: 0` |
| R1, populated | refused after `outside_scandir: 1` | refused, `outside_scandir: 0`, victim intact |
| R3 | stale helpers `accepted`, `run_id_reused: true`, resume false skips, stale builder wrote | `reentry: refused: a Writer is single-use ...`, `stale_builder_wrote: false` |

The new test file was also run against a scratch copy of the round 1 `store.py` and `_fs.py` (taken with `git show :path`): `32 failed, 14 passed` (`tests_before.txt`). The 14 that passed are class checks that round 1 already met: link-out at leases, holds, the registry and `sync_state`; a tampered manifest at pin and at publish; the data-store directory.

**Tests** (new `tests/test_storage_review_r2.py`, 47 cases):
- **R2 class:**
  - `commit()` is refused over an artifact referenced by a validated snapshot, by a published snapshot in a later session, by an unfinished build, and for a table artifact; the recorded bytes may be written again; a collected snapshot no longer freezes its artifact.
  - Publish is refused after a same-size, same-mtime tamper of an artifact, a table file or the manifest, with the hash cache warm.
  - Rollback after a tampered artifact, resume (`done`) after a tampered checkpoint artifact, and pin after a tampered manifest are each refused.
- **R1 class:** a junction out of the store, to an empty and to a populated folder, at each traversal entry point (`artifacts/`, `artifacts/blob/`, `leases/<sid>`, `holds/<sid>`, `registry/sync_state`, `registry/`, `snapshots/`, `snapshots/.building-*`). An audit hook asserts that nothing outside is listed and that the outside folder is unchanged. Also covered: a link back into the store, a link inside a snapshot being collected (skipped, then retried), and a data-store listing.
- **R3 class:** a re-entered Writer is refused; a new session gets a new run id, and `resume: false` redoes the earlier output; each of the 16 helper calls is refused after a refused re-entry, which leaves no lock behind.

**Results** (through `test_slot.py`, venv python, `PYTHONPATH=src`):
- full suite `pytest -q -s -p no:cacheprovider --basetemp=.tmp/T-006/pt tests`: `953 passed, 25 skipped in 168.55s` (906 before plus 47 new; the same 25 T-005 skips), log in `full_suite.log`;
- lock hold times: `{"reader": {"n": 50, "median_s": 0.0065, "max_s": 0.1807}, "publisher": {"n": 4, "median_s": 0.0845, "max_s": 0.0856}}`. The publisher median rose from 0.0624 s because publish now hashes every file under the store lock;
- round 1 probes (`review-r1/rerun_probes.py`): output identical to round 1's `after.jsonl` apart from a BOM (`r1_probes_after_r2.jsonl`);
- `ruff check`: `D:\Project_github\prompt-enhancer-pipeline\.venv\Scripts\python.exe: No module named ruff` (not run).

**Mutants** (`.tmp/T-006/review-r2/mutants_r2.py`, scratch copy of `src/`, child processes with `CREATE_NO_WINDOW`, log `mutants_r2.log`): 13 run, 13 killed.
- `store.py` (10): publish `verify_snapshot`, fresh hash, artifacts in full verify, commit immutability, the cache reset in `reference_artifacts`, the cache reset after GC, the `real` check, the walk in `_remove_tree`, GC skipping a link, the single-use Writer.
- `_fs.py` (3): the `real_inside` comparison, the table write check, the blob write check.
- One defect found while writing the mutants: a cache reset in `finish` was redundant, because the cache already held that build's entries. It was removed instead of being tested.

**Configuration audit delta:** no new key, no changed key and no changed example. New retained literal: `LIVE` (`validated`, `published`, `retired`), which is protocol-bound to the contract's `snapshots.state` values. The new `check` parameter is an internal API, not configuration.

**Open issues:**
- **Publish cost.** Publish and rollback now hash every snapshot file and artifact while holding the store lock, so the lock is held for a time proportional to the snapshot's size. A reader pin waiting longer than `storage.lock.timeout_s` behind a large publish times out. Setting that value per machine is already `deploy_required`; this round makes it depend on snapshot size, a fact for the deployment owner.
- **Read after hash.** A full verify hashes a table file and then reads it again, which leaves a small window for an outside writer. The store lock does not stop outside writers in any case.

**not_run:** the Linux path (`fcntl`, the symlink branch of the link tests, directory fsync; CI only); ruff (not installed); `probe_writer_death.py` (this round does not change writer death).

## Fix round 3

Step 0 (last fix round inside a started task, binding brief from the PM): reproduce every lead on a round 2 baseline → one identity rule and one Writer mutex at every entry point → class tests, mutants, full suite. No open questions.

**Root cause** (the PM's reading, confirmed by reproduction):
- **Identity.** The store identified a file by the spelling of its relative path, while NTFS maps many spellings to one file. Facts measured on this machine (`review-r3/fs-facts-r3.json`):
  - realpath returns the on-disk case and drops a final dot or space;
  - `name:stream` writes succeed;
  - NFD and NFC are two files on NTFS (they are one on macOS);
  - 8.3 names are not generated on D:.
- **Ordering.** A Writer's operations interleaved across threads with no rule, so one thread's check (for example the immutability check in `commit()`) could be undone by another thread's act before its own act.

**Before and after** (`review-r3/probe_r3.py`; before ran on a round 2 baseline rebuilt from Codex's round 1 copies plus their deltas, and the rebuilt `store.py` was byte-identical to the round 2 file; outputs in `before-r3.jsonl` and `after-r3.jsonl`):

| Finding | Before | After |
| --- | --- | --- |
| R2-A commit (Codex) | uppercase alias referenced; commit `evil` accepted; CURRENT fails verification | alias refused (`not a stored artifact path in its generated form`); commit refused; CURRENT intact |
| R2-A GC (Codex) | GC removed the artifact CURRENT references | alias refused; nothing removed; CURRENT intact |
| R2-A alias matrix | case, final dot and final space accepted | all 8 forms refused |
| R2-B (Codex) | CURRENT verification fails (`checksum differs`) | commit, reference and publish serialized; CURRENT intact |
| finish gap (Gemini) | finish validated a snapshot whose artifact differs | `validated_intact: true` |
| publish vs commit (Gemini) | `publish_completed_while_commit_paused: true` | `false` |
| GC vs reference (Gemini) | reference accepted, then GC deleted the file | GC finishes first; the reference is then refused, so nothing dangles |
| add_file case collision (Gemini) | two manifest entries, one file on disk (`two`) | second name refused; disk `one` |
| hold case collision (Gemini) | `REQ-A` replaced and released `req-a`'s hold | both refused; `req-a.parquet` kept |
| registry sweep alias (lead from the Gemini registry note) | authoritative registry deleted once its file was renamed on disk to another case | file kept |
| lease sharing violation (Gemini) | GC crashed with `PermissionError` | lease counted live; snapshot kept |

Dropped with evidence:
- **8.3 false positive in `real_inside` (Gemini).** `GetShortPathNameW` returns the long name on D:, so the case cannot occur here. Structurally it cannot occur either: the store root is resolved (long names) at construction, and every segment containing `~<digit>` is now refused.
- **`Checkpoints.done()` gap (Gemini).** A tamper after `done()` returns is refused by the next acting point: publish reports `checksum differs`. `reference_artifacts` records the bytes, and `finish` and `publish` verify them again. The probe records this.

**Changes:**
- **One segment rule** (`_fs.check_segment`, applied by `inside()` to every path the store touches):
  - refused: `''`, `.`, `..`; control characters and Windows' forbidden characters (`<>:"|?*\/`); a final dot or space; device names (`con`, `nul`, `com1`, `CON.txt` and so on); `~<digit>`; any text that is not NFC.
  - An alias is refused, never rewritten.
  - The list of device names and forbidden characters is `protocol-bound` (Windows naming rules), not a word list.
- **One identity rule** (`identity.py`):
  - **Store-generated names** have a single spelling. Artifact paths must match the exact generated form (`check_artifact_path`: lowercase kind, prefix equal to the key's first two hex digits, 64 lowercase hex, suffix); `commit()` builds them with `artifact_path()`. Registry, segment and lease names use a one-case alphabet. Snapshot ids keep their exact pattern, whose letters are fixed per position.
  - **Caller-chosen names** pass `check_relative_name`, and within a namespace they must not share `identity_key` (NFC plus full case folding). This applies to snapshot files and tables, folder prefixes against file names, and request ids.
  - `identity_key` was checked against NTFS (`case-facts-r3.json`): every pair NTFS maps to one file shares a key. It over-refuses `ß`/`SS` and `k`/KELVIN SIGN. An earlier `upper()` step was removed because the probe showed it added only over-refusal.
- **Admission points routed through the rule:**
  - `reference_artifacts`: exact form, and `store.real()`, so the file on disk must carry that spelling.
  - `commit` (kind, key).
  - `add_file` and `write_table` (namespace, plus a disk check that the target does not exist in the new build directory).
  - Manifest rows on every read: the namespace for files, exact form and no repeats for artifacts.
  - Holds: the file is named by the request id's identity key; another request whose id differs only in case is refused, both for `hold` and `release_hold`.
  - Every `Checkpoints` method (`check_artifact_key`), and `sync_state` rows on read (output keys and GC marks in exact form).
  - GC marking: a name on disk that is not the generated form is reported in `skipped` and never marked or deleted.
  - Registry sweep: exact registry names only.
  - Lease directory names: must be an exact snapshot id, else GC refuses.
  - `real_inside` compares the resolved path with the spelled one exactly (no `normcase`). realpath returns the on-disk spelling, so a case alias or a dropped final dot is refused on every traversal.
- **One serialization rule.** `Writer._mutex` (an `RLock`) and the `_writer_operation` decorator hold it from check to act. It covers:
  - Writer: `checkpoints`, `begin_snapshot`, `publish`, `rollback`, `gc`, `__enter__`, `__exit__`;
  - Checkpoints: `state`, `done`, `commit`, `queue`, `fail`, `load`;
  - SnapshotBuilder: `write_table`, `add_file`, `reference_artifacts`, `write_manifest`, `finish`.

  Holds and pins are not Writer operations; they serialize on the store lock, as round 1 established.
- **Lock order** (documented in the `store.py` docstring): Writer mutex → WRITER file lock → store LOCK → lease lock (a leaf). No code that holds the store lock calls a writer operation; `test_lock_order_never_takes_the_writer_mutex_under_the_store_lock` instruments both and runs every operation.
- **Lease and hold files:**
  - `FileLock.try_acquire` opens the file inside its error handling. `PermissionError` means "not acquired": GC counts the lease as live, and `pin` now uses `acquire()`, so it waits up to `timeout_s` and then raises `StorageError`.
  - Hold files are parsed from bytes read through a closed Python handle (`_read_hold`). The first full-suite run failed once with WinError 32 deleting a hold file just after it was read through PyArrow; whether PyArrow's handle or a scanner's caused it was not determined. After the change, three reruns of the hold tests passed and the full suite passed.

**Tests** (new `tests/test_storage_review_r3.py`, 134 cases; `tests/test_storage_concurrency.py` publisher now records its exceptions):
- **Aliases:** every admission point with each of 9 alias forms (case, final dot, final space, NFD, `:stream`, device name, device name with extension, `~1`, `\`). The points: `reference_artifacts`, `commit` (kind and key), `add_file`, manifest rows (file and artifact), holds (11 ids, including release), and the five `Checkpoints` methods. Also tested:
  - file/folder collisions, a repeated artifact, a name already on disk, a wrong key prefix;
  - a canonical reference whose file on disk has another case;
  - a GC run with an on-disk case rename and a foreign file (both skipped, nothing deleted);
  - the registry case rename;
  - `.BUILDING-` and uppercase lease directories.
- **Races:** 12 Writer operations × {publish, gc} racing from a second thread. The operation pauses at its first file replace or hash, and the test asserts that the racer is still blocked after 0.5 s, that the operation succeeds, and that CURRENT then verifies. Also the lock-order instrumentation, and the lease sharing violation (simulated `PermissionError`, plus a real exclusive Windows handle): live, no uncaught exception.
- **Run against the round 2 baseline:** `87 failed, 40 passed` (`tests_before_r3.txt`). The 40 that pass are forms round 2 already refused (`:`, `\`, missing files) and the 8 race cases whose pause falls inside the store lock (begin_snapshot, publish, rollback, gc).

**Results** (through `test_slot.py`, venv python, `PYTHONPATH=src`, `-W error::pytest.PytestUnhandledThreadExceptionWarning`):
- full suite: `1083 passed, 25 skipped in 229.54s`, exit 0 (`full_suite_r3.log`). The first run gave `1 failed, 1082 passed`, the hold-file WinError 32 described under Changes;
- lock hold times: `{"reader": {"n": 50, "median_s": 0.007, "max_s": 0.2234}, "publisher": {"n": 4, "median_s": 0.0565, "max_s": 0.0941}}`;
- `ruff check`: `D:\Project_github\prompt-enhancer-pipeline\.venv\Scripts\python.exe: No module named ruff`.

**Mutants** (`review-r3/mutants_r3.py`, scratch copy of `src/`, children with `CREATE_NO_WINDOW`; `mutants_r3.log`): 16 run, 16 killed.
- `store.py` (8): no Writer mutex, namespace lookup by spelling, build-dir disk check, hold collision, registry sweep by prefix, GC skip of non-generated names, reference via `path` instead of `real`, lease directory names.
- `_fs.py` (5): lease open `PermissionError`, final dot or space, device names, NFC, exact `real_inside` comparison.
- `identity.py` (3): segment rule for caller names, artifact prefix, case folding.
- A first run had 2 survivors, both equivalent mutants: `inside()` already refused device names in the hold file name and the artifact kind folder. The duplicate checks they mutated were removed.

**Configuration audit delta:** no new key, no changed key, no changed example. New retained literals:
- `_DEVICE`, `_WINDOWS_FORBIDDEN`, `_SHORT_NAME`: `protocol-bound` (Windows naming rules);
- `_ARTIFACT_PATH`: `artifact-bound` (the generated layout);
- `PAUSE_S = 0.5` and the 10 s release bound in the race tests: `test-only`.

**Residual risks:**
- **Outside actors.** A process other than the store writing into the store directory is not prevented by any lock. It is detected at the next acting point: publish, rollback and resume re-hash.
- **Other file systems.** `identity_key` was measured on NTFS only. The macOS and Linux case-insensitive modes are covered by casefold and NFC by construction, not measured.
- **The Writer mutex serializes only one Writer object.** Two Writers in one process exclude each other through the WRITER file lock (msvcrt locks conflict between handles, T-006 probe 9; `fcntl.flock` on Linux CI).
- **Publish cost** (round 2) is unchanged: the store lock is held while the snapshot is hashed.

**not_run:** the Linux path (CI only: `fcntl`, symlinks, directory fsync, case-sensitive behaviour of the alias tests); ruff (not installed).

## Fix round 4 (Fable)

Step 0 (extra fix round inside a started task, binding brief from the PM, decision 107): reproduce F1 on the round 3 code → make the round 2 link test the one path rule of every operation → class test, mutants, full suite. No open questions: the brief fixes the approach (extend the existing containment helper, no per-call special cases).

**Root cause** (the PM's reading, confirmed by reproduction). The store had two path rules.
- `KnowledgeStore.real` (round 2) refused any link, but only traversals called it.
- `KnowledgeStore.path` refused a link only when it left the store, and every single-path operation called that one.

So a link that stayed inside the store was followed. The immutability check and the identity rule compared the spelled name `artifacts/alias/...`, while the operating system wrote `artifacts/blob/...`. The same gap held for every other operation that took its path from `path`, and for the snapshot helpers that resolved a path once and reused it later.

**Changes** (`src/prompt_enhancer/storage/store.py`; `_fs.py` only gains a docstring):
- **One path rule.** `KnowledgeStore.path` now is the round 2 link test (`_fs.real_inside`): inside the store root, and the resolved path equals the spelled path exactly. `real` is removed; traversals (`_children`, `walk`, `_remove_tree`) and single-path operations call the same `path`. A link is refused, never followed, whether it leaves the store or stays inside it.
- **Paths are resolved when used, never stored.** Three places kept a resolved directory and joined names to it later with a second, weaker check (`inside(directory, ...)`); they now keep the store-relative string:
  - `_snapshot_dir` returns `snapshots/<id>`; `_manifest`, `_entry_path` and `_verify_dir` take that string, so pin, `finish`, publish and rollback check every file from the store root.
  - `SnapshotBuilder.relative` replaces the stored directory (`directory` is a property that resolves it); `write_table`, `add_file`, `write_manifest` and the rename in `finish` take their path at the act.
  - `PinnedSnapshot` resolves each read from the store root.
- **Under the lock that acts.** Four acts used a path taken earlier; each now takes it again at the act:
  - the `WRITER` lock file is built in `Writer.__enter__`, not in the constructor;
  - `PinnedSnapshot.release` takes the lease path again before it deletes the lease (the OS lock is released first, in every case);
  - the GC artifact pass deletes `self.store.path(relative)`, not the path listed when GC began;
  - `Checkpoints.done` resolves the artifact outside its "corrupt means redo" handler, so a link is refused instead of answered "redo it".
- **GC decides before it changes anything.** `gc()` reads the references of every live snapshot before the first mark, next to the artifact listing of round 2, so a link at a live snapshot refuses the whole GC with the registry untouched.
- **Data store.** `DataStore.append` uses the same rule (`real_inside`), as `DataStore.read` already did.
- Operations covered by the one helper: `commit` (table arm and file arm), `reference_artifacts`, `done`, `load`, `queue`, `fail`, `write_table`, `add_file`, `write_manifest`, `finish`, `begin_snapshot`, `publish`, `rollback`, recovery, GC (leases, holds, snapshots, artifacts), `pin`, pinned reads, lease release, `hold`, `release_hold`, the registry and its pointer, `CURRENT`, `LOCK`, `WRITER`.

**Research evidence** (experiment, `review-r4/probe_links.py`, output in `link-facts-r4.json`; Windows 11, Python 3.12.10):
- `os.path.islink(junction)` is `False`, while `os.readlink` and `os.path.realpath` resolve it. An `lstat` test for a symlink therefore misses junctions on Windows, so the rule compares the resolved path with the spelled one, which the brief allows.
- A file symlink cannot be created here: `OSError: winerror 1314` (privilege not held). File-level links are tested only where they can be made.

**Before and after** (`review-r4/probe_r4.py`, F1 as Codex reproduced it plus the class; before ran with `PYTHONPATH` on a copy of the staged round 3 modules taken with `git show :path`; outputs in `before-r4.jsonl` and `after-r4.jsonl`):

| Case (the link stays inside the store) | Before | After |
| --- | --- | --- |
| F1 `commit`, file arm | `accepted`; published bytes changed; CURRENT: `checksum differs from the manifest` | `refused: ... is a link or another spelling of a stored name`; bytes unchanged; CURRENT verifies |
| F1 `commit`, table arm | `accepted`; published bytes changed; CURRENT: `size 4655 differs from the manifest (4634)` | refused; bytes unchanged; CURRENT verifies |
| rollback to a snapshot directory that is a junction | `accepted`, CURRENT moved | refused, CURRENT unchanged |
| pin of a published snapshot whose directory is a junction | `accepted: b'index a'` | refused |
| `add_file` through a junction in the build directory | `accepted`, written into the other folder | refused, nothing written |
| GC with a retired snapshot directory linked to the published one | `accepted` (skipped that snapshot after marking it) | refused before any mark |
| data store `append` through a junction | `accepted`, segment written into another table's folder | refused, nothing written |

**Tests** (new `tests/test_storage_review_r4.py`, 72 cases):
- **F1** as reported, both arms: the junction exists before the snapshot is built; `commit` through the alias is refused, no byte under the store changes, and `verify_snapshot(CURRENT)` passes.
- **Class test**: 31 single-path operations × 2 link targets (a directory outside the store, and `elsewhere/` inside it). A directory that already exists is moved to the target first, so the operation would succeed if it followed the link. Each case asserts the refusal, compares every file under the store and the target byte for byte before and after, then removes the link and checks that the store still serves CURRENT.
- **Path taken again at the act**: a link that appears between GC's listing and its artifact pass is refused, and the orphan is collected once the link is gone; a lease whose directory became a link after the pin is not deleted through it.
- **A link to an empty sibling as the last component** (`leases/<new> -> leases/<old>`): refused by GC and by `pin`.
- **Data store**: `append` under a linked `traces` and `traces/request_traces`, both targets.
- **File symlinks** (`CURRENT`, `LOCK`, `WRITER`, `registry/REGISTRY`, an artifact, a snapshot file): one test that skips where symlinks cannot be created (skipped here).
- **Run against the round 3 baseline:** `34 failed, 37 passed, 1 skipped` (`tests_before_r4.txt`); after: `71 passed, 1 skipped` (`tests_after_r4.txt`). The 37 that pass on round 3 are 30 cases whose link leaves the store, which rounds 1 and 2 already refused, and 7 inside-the-store cases that sit on a round 2 traversal or on `reference_artifacts` (round 3).
- **Earlier tests changed** (`tests/test_storage_review_r3.py`, two tests): `_manifest` now takes a store-relative directory, so both write their manifest under the store root. One test gained a control assertion (the same manifest without the alias is read), because its bare `pytest.raises(StorageError)` would otherwise also pass on a wrong path.

**Results** (through `test_slot.py`, `D:/Project_github/prompt-enhancer-pipeline/.venv/Scripts/python.exe`, `PYTHONPATH=src`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`):
- full suite `pytest -q -s -p no:cacheprovider -W error::pytest.PytestUnhandledThreadExceptionWarning --basetemp=.tmp/T-006/review-r4/pt tests`: `1154 passed, 26 skipped in 316.33s`, exit 0 (`full_suite_r4.log`). That is 1083 plus 71 new; the 26 skips are the 25 T-005 skips and the file-symlink test;
- the eight storage test files of rounds 0 to 3 alone, before the new file existed: `277 passed in 152.90s`;
- lock hold times in this run: `{"reader": {"n": 50, "median_s": 0.0161, "max_s": 0.1488}, "publisher": {"n": 4, "median_s": 0.0892, "max_s": 0.0934}}`;
- `git diff --check` (work tree against the index) and `git diff HEAD --check`: clean, exit 0. `git diff --cached --check` still reports line 503, because the index holds the round 3 copy and a worker does not stage; the work tree line is fixed;
- child processes keep `CREATE_NO_WINDOW` (`tests/test_storage_concurrency.py:22`, `.tmp/T-006/mutants.py:27`); this round starts no new child process in tests;
- `ruff check` and `ruff format --check`: `D:\Project_github\prompt-enhancer-pipeline\.venv\Scripts\python.exe: No module named ruff`.

**Mutants** (`review-r4/mutants_r4.py`, scratch copy of `src/`, children with `CREATE_NO_WINDOW`; `mutants_r4.log`): 13 run, 12 killed at first, 13 after one added test.
- `store.py` (10, each site joins the root instead of calling `path`): commit file arm, commit table arm, the builder's target, lease release, the GC delete, the GC read before any mark, `done` refusing instead of redoing, the hold path, lease creation, data store append.
- `_fs.py` (3, the rule itself): no comparison (the round 3 behaviour), the spelled path compared with itself, the last component not compared.
- **Survivor:** "the last component is not compared" (`mutants_r4.log`: `117 passed, 1 skipped`). No case had a link whose target shared its parent and was empty. After `test_a_link_to_an_empty_sibling_is_refused_as_the_last_component` the rerun gave `('real_inside: the last component is not compared', 'killed', ['1 failed, 66 passed in 44.37s'])` (`mutant_survivor_r4.log`).
- Not mutated: the path of `WRITER`, `LOCK` and `CURRENT`. Only a file symlink reaches them, so such a mutant cannot be killed on this machine.

**Configuration audit delta:** no new key, no changed key, no changed example, no new retained literal in `src/`. Internal API changes, none of them configuration: `KnowledgeStore.real` is removed; `_snapshot_dir`, `_manifest` and `_verify_dir` work on store-relative strings; `SnapshotBuilder.directory` is a property; `PinnedSnapshot.directory` became the private `_directory`. Test-only literals: `elsewhere`, the fixed `.building-` name.

**Behaviour change to note:** a link inside `storage.kb_store` or `storage.data_root` that points to another place inside the same store is now refused. A link that left the store was already refused since round 1, so no layout that worked across disks is lost.

**Residual risks:**
- **Check, then act.** The rule checks the path and then calls the OS by name; it holds no directory handle. An outside process that puts a link in place between the two is followed once. No store lock stops outside processes; publish, rollback and resume re-hash, so changed bytes are found at the next acting point.
- **Hard links** are not links for this rule. Every store write goes to a temporary file and replaces the name, so a hard link is never written through; lock files are opened but never written.
- **The store root and what is above it** are resolved once, when the store is built, by the configuration containment check; the rule covers what is below the root.
- **Publish cost** (round 2) is unchanged.

**not_run:**
- The Linux path (CI only): `fcntl`, directory symlinks in place of junctions, directory fsync.
- The file-symlink test and mutants of the `WRITER`, `LOCK` and `CURRENT` paths: file symlinks need a privilege this machine does not grant (`winerror 1314`).
- Ruff: not installed.
- Codex's `probe_junction.py` itself: it writes under the reviewer's folder `.tmp/T-006-rereview4`; `probe_r4.py` repeats its steps under `.tmp/T-006/review-r4/`.

**Changed files:** `src/prompt_enhancer/storage/store.py`, `src/prompt_enhancer/storage/_fs.py` (docstring), `tests/test_storage_review_r3.py`, new `tests/test_storage_review_r4.py`, this task file (this section, and the trailing whitespace at line 503). All unstaged on top of the staged round 3 change. Kept under `.tmp/T-006/review-r4/`: `probe_r4.py`, `before-r4.jsonl`, `after-r4.jsonl`, `probe_links.py`, `link-facts-r4.json`, `tests_before_r4.txt`, `tests_after_r4.txt`, `mutants_r4.py`, `mutants_r4.log`, `mutant_survivor_r4.py`, `mutant_survivor_r4.log`, `full_suite_r4.log`, `cleanup_r4.py`. The scratch copies, test trees and stores were removed after a check that they held no link.

## Noticed, not done

- `docs/model-storage.md` does not say that the stores refuse links below their roots; one sentence there would tell an operator why a linked folder is refused (docs are read-only for this round).
- `SnapshotBuilder` admits a name into its namespace before the path rule runs, so a name refused for a link stays taken and a retry of the same name in that build is refused as a duplicate. The build is refused either way; behaviour unchanged since round 3.

- `cards.summary` is nullable in `concept_card.v0`, but K1 and K4 consume it; this goes to T-010 (PM decision).
- The contract key of `concept_text_embeddings` (`concept_id`, `text_kind`, `embedder_revision`) collides when a concept has several aliases. This layer adds `text_hash` to the key; the K1 owner (T-008) should confirm, or change the contract.
- `images.decode_status` has no enum in the contract; it is a free string until T-008 defines its values.
- The JSON Schema `pattern` on `sync.gc.retention` accepts a trailing newline, because the loader matches with Python's `re.search` and `$` matches before a final newline. The store refuses it; an anchored pattern or `\Z` in the schema would close this at load.
- Python 3.12's `zoneinfo` needs `tzdata` on Windows; any future code that converts named zones has the same trap.
