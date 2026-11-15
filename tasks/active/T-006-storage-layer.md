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

Fill before the first edit: every storage and lifecycle key read, its schema, its example value, its override and refusal tests, and its classification.

## Step 0

(The implementer writes Step 0 here and stops.)

## Steps

## Completion report

## Noticed, not done
