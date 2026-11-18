# Execution: commands, sync, snapshots, resume

How the pipeline runs end to end. Data shapes: [contracts](../contracts/README.md). Requirements: R-009, R-014, R-016, R-017, R-020. Decisions: 12, 17, 19, 20. K0-K5 sync is implemented with CPU contract tests and HTTP doubles; deployment performance and quality are unmeasured. The design draws on [N124](note/N124-incremental-indexing.md), [N125](note/N125-snapshots-and-vector-deletion.md) and [N126](note/N126-content-addressed-caches.md).

## Where things run (R-020)

- **Everything works on the dev machine first**, end to end, on the example knowledge base and the agent-built scenarios: `kb sync`, admin profile registration, both tasks with retrieval on and off, validation and evaluation. A feature is done only when it runs there. Dev results show that code works, never quality ([dev-machine](dev-machine.md)).
- **The user runs the deployment.** The agent never logs in to it; it writes a run guide (commands, configuration overlay, expected outputs, how to check VRAM, resume and roll back) and reads the results the user pastes or places in shared storage ([serving](serving.md)).
- **Models** are reached over HTTP through the backend each `vlm.roles.<name>` binds to (R-018); request fields follow [VLM_CALL_RULES](rules/VLM_CALL_RULES.md).

## Commands

Each command refuses at start when a key it reads still holds a sentinel or is missing, naming the key, because a silent default hides a decision nobody made (R-015).

| Command | Does | Needs | Refuses |
| --- | --- | --- | --- |
| `kb status` | shows the stored snapshots, the current one and its row counts; it does not look at the source folders, so `live_source_changes` is `not_checked` (use `kb sync --plan` for pending changes) | `storage.kb_store`, `storage.lock` | nothing: a missing store is reported (`store_present: false`, exit 0) |
| `kb sync [--plan] [--force]` | runs K0 to K5 and publishes a snapshot; `--plan` stops after the plan; `--only <folder>` is refused (a scoped sync would treat the other folders as removed) | `knowledge.*`, pinned models and revisions for the roles K1 to K4 use, `sync.*` | a plan over the mass-deletion guard without `--force`; unpinned models (`pin_required`) |
| `kb rollback <snapshot>` | moves the current pointer to a retained snapshot | the snapshot still retained | a snapshot already collected |
| `kb gc` | removes unreferenced artifacts and snapshots past retention | `sync.*` retention keys | artifacts a retained snapshot references |
| `kb audit` | samples card facts and stability labels for human checking (E16) | a published snapshot | nothing |
| `kb show <concept>` | prints a concept's card, aliases and folders by concept id or name (no lint: the snapshot tables store none) | a published snapshot | unknown concept (reported) |
| `profile register` | drafts the manifest and conflict list for a system prompt (P) | `profiles.root`, the `manifest_drafter` backend | nothing is activated until the admin confirms |
| `profile check` | runs conflict lint and golden tests; activates on success | a confirmed manifest and a decision for each conflict | activation while a conflict is `block_activation` or a golden test fails |
| `enhance once --request <file>` | runs S0 to S6 for one `request.v0` file and prints the envelope as one JSON line; retained attachments are written first (below) | an active admin profile; with retrieval on, a published snapshot; `serving.http.max_request_bytes` | a task the profile does not support (decision 5); a bad request file |
| `enhance serve` | serves S0 to S6 over HTTP; **not yet built** (T-016 phase 3): refused with exit 2 | as `enhance once`, plus `serving.*` | `deploy_required` values left in `serving.*` |
| `eval build` | has agents generate scenarios and answer keys (G); owned by T-003, refused with exit 2 here | a published snapshot, the `scenario_generator` backend, a call budget for paid backends (`ask_user`) | a paid backend without a budget |
| `eval run`, `eval import` | run scenarios through the pipeline and score them; **not yet built** (T-016 phase 4): refused with exit 2 | built scenarios, an active profile | scenarios built from another snapshot unless named |
| `eval fit` | fits `fit_from_*` values on the `fit` split and writes an evidence report; owned by T-030, refused with exit 2 here | results on the `fit` split from the deployment's models | fitting on the test split, or on dev-machine runs |

**Command line (T-016).** Every command takes `--config <profile>` (and `--overlay`, `--project-root`); paths start at the project root and nothing outside it is read. For the commands added by T-016 the `--config`, `--overlay` and `--request` paths follow the stores' no-link rule: a link or junction on the way is refused, also when it stays inside the root, and the request file is opened without following links and read in bounded chunks. **Exit status:** 0 done or delivered (an honest `failed` or `degraded` envelope is still delivered); 1 an operational failure (a model call, a store, a delivery failed); 2 a refusal made before any side effect (a bad argument, request or configuration value, or a command this build does not serve). `kb sync` and `kb audit` keep their earlier behaviour: a configuration or store error exits 1. JSON goes to stdout only on success, one line, UTF-8; diagnostics go to stderr and name a field and a keyword, never a prompt or an image.

**`enhance once` attachments.** When the envelope carries `attachments`, each image is first written to `<data_root>/attachments/<trace_id>/<sha256 hex>` as the exact snapshot bytes (hash-checked against its `image_id`), next to `index.json` (hashes, sizes, expiry); only then is the envelope printed and the response acknowledged (R-024). If a copy or the stdout write fails, the command exits 1, prints no envelope and does not acknowledge. A later run removes directories whose index has expired (`runtime.trace.retention_s` after delivery); it never follows a link or removes a directory it cannot read as its own. `serving.http.max_request_bytes` bounds the request file (and later the HTTP body): the dev profile holds a proxy taken from the retained-image bound (`sync.retained_images.max_image_bytes`) as base64 plus slack, because `request.v0` sets no length limit of its own; the example and the vLLM overlay hold `deploy_required`.

## The `kb sync` flow (R-014, decision 17)

1. **Scan** the whole source tree every time, because an incremental listing cannot see deleted sources ([N124](note/N124-incremental-indexing.md)). Size and modification time pick the files to hash; the hash decides.
2. **Diff** against the current snapshot: added, changed, moved, removed images and folders, and changed info files and `kb_overrides.yaml`.
3. **Plan:** print the work and the number of model calls per role. `--plan` stops here.
4. **Guard:** stop when the plan removes more than `sync.mass_deletion_guard.max_removed_fraction` (`design_required`) of the knowledge base, for example when the source drive is not mounted; `--force` overrides. The removal fraction is unmatched removed folders divided by folders in the previous snapshot, or zero if it has none; confirmed moves are not removals. Ambiguous matches count as removals. A full scan that lists too little would otherwise delete everything ([N124](note/N124-incremental-indexing.md)).
5. **Execute** per artifact in the order K1 (image embeddings), K2 (first the info-file assessment and the naming of folders without a usable info file), K3, K4, K1 (concept-text embeddings), resumable; an undecodable file is recorded, skipped and reported.
6. **Build and validate** the new snapshot beside the current one (K5): referential integrity and sample queries.
7. **Publish** by moving the current pointer atomically; retain `sync.snapshots.keep` snapshots for rollback.

`--plan` reports the guard verdict and exits 1 when the guard would refuse execution, or 0 when it permits it, including with `--force`. It hashes each selected file because checkpoint reads currently require a writer session that may recover or compact the store; plan preserves the store without taking that writer session.

`prompt_enhancer.kb_sync.sync_knowledge` validates the complete consumed profile before constructing a store or scanning. Disabled retrieval returns `status: skipped`, `reason: retrieval_disabled`, exit 0 after general structure validation, without resolving KB/store paths; unused stage sentinels remain allowed. Enabled execution uses one writer across K0, K1 images, K2 naming, K2 observation, K3, K4, K1 text and K5. Every failed item blocks publication, including partial K4 alias results. K1 text inputs are sorted distinct exact nonempty names, assigned descriptions, accepted aliases and card summaries; pending aliases are excluded.

Completed source/configuration/prompt/schema/tool identity lives in the snapshot scan summary. An unchanged repeat verifies current checksums and saved K5 semantics before returning the existing snapshot ID; it makes no model/probe calls or persistent file changes. Changed and interrupted runs reuse intact semantic item checkpoints, assemble current rows, validate K5, finish and publish last. Plan reports role work as conditional on later model replies. This command does not run GC implicitly; configured retention remains enforced by storage GC.

With `knowledge.on_source_removed: remove` (the default) a deleted source loses its knowledge at the next sync; until then the database and running requests are unaffected, because the request path reads only the snapshot (decision 17).

K0 resolves the configured source root at startup and opens entries below that root without following links. It reports and refuses links it encounters; replacement of a path component after validation remains a race and is not a proof of containment against every concurrent replacement. The resolved and configured root spellings are recorded in the scan report.

## What changed, what is reprocessed, what is not

| Change in the source tree | Reprocessed | Not reprocessed |
| --- | --- | --- |
| Image added to a folder | its hash and embedding; its observation if it becomes a representative; cards of the concepts the folder declares | every other image |
| Image bytes changed | as a removed image plus an added one | |
| Image or folder renamed or moved | paths and `folder_images` rows only | embeddings, observations |
| Image removed | removed from the index; cards of its concepts recomputed | other images of the folder |
| Folder removed | its images removed; a concept no other folder declares leaves the index | concepts other folders still declare (removing folder `kimono_donut` keeps `kimono` while folder `kimono` exists) |
| Images added to or removed from a model-named folder beyond the configured share (`knowledge.naming.rename_on_change`, `design_required`) | naming of that folder, then observations and cards of the concepts it names | other folders |
| A model-named folder gets an info file | the info file is assessed; when usable the folder switches to it (decision 9); observations for names that differ, cards, aliases | image embeddings |
| Info file line 1 changed (concepts added or removed) | observations of the folder's images for added concepts; cards of added and removed concepts; aliases | image embeddings |
| Info file line 2 changed (descriptions) | the `curated` part of the card, concept text embedding, lexical index entries | observations, because the observer prompt carries concept names only |
| `kb_overrides.yaml` changed | cards of the concepts it names | |
| Observer model, observer prompt or facet schema changed | observations and cards, by tier ([K2](stages/K2-observe.md)) | embeddings |
| Embedder changed | all embeddings, into a new index | observations, cards |

Cards are recomputed from the current image list, never accumulated, because rows removed at the source do not disappear from an incrementally updated target on their own ([N124](note/N124-incremental-indexing.md)).

## Artifact keys, stage hash and resume (R-017)

- **Artifact key** = hash of the input content plus the revisions of the model, prompt and schema that produce it ([N126](note/N126-content-addressed-caches.md)). A rename keeps the content and so the key; a new model, prompt or facet schema changes the key and the old artifact simply stops matching. Examples: an observation is keyed by `image_id`, concept, observer model and processor revision, prompt revision and facet schema version; a card by its observation keys, the info-file and override hashes, the card builder revision and the stability policy version.
- **Stage hash** = hash of the configuration keys a stage declares it reads, the model revisions it uses and its code version. Changing a policy key reruns only the stages that read it.
- **Resume:** progress lives in `sync_state`, written after each artifact's output (commit last). Intact completed artifacts are skipped when resume permits it; referenced artifacts remain reusable when resume is off. A half-written artifact has no state row and is redone. Failed or deferred attempts never replace a durable done checkpoint, including one from another run or helper; the current item error or deferral stays in the stage report. This preservation rule does not make a corrupt artifact reusable, change skip eligibility, or prevent a successful recomputation from committing its result.

## Snapshots, publish, rollback, garbage collection

A snapshot is an immutable directory under `storage.kb_store` with a manifest: the tables (`folders`, `concepts`, `folder_concepts`, `images`, `folder_images`, `image_embeddings`, `concept_text_embeddings`, `observations`, `cards`, `aliases`), the serving index built from them, and copies of the knowledge-base images the `attach` channel may send. Publishing moves one pointer; `kb rollback` moves it back. Readers pinned to an older snapshot keep a consistent view while a new one is written ([N125](note/N125-snapshots-and-vector-deletion.md)). Exact blocked numpy search and character n-gram postings are derived per immutable snapshot (decision 134; [K5](stages/K5-index.md)); the source tables remain authoritative. Unreferenced artifacts are kept for `sync.gc.retention` before `kb gc` removes them, so restoring a file removed by mistake costs no model call.

An asynchronous interrupt between returning a pin and recording it in the caller can leave a lease behind. A held lease protects the snapshot from GC until the lease is released; GC removes an unlocked lease as stale. Lease files currently contain no owner identity, so GC cannot determine whether an unlocked lease's originating process is alive. Owner-identity reclamation is deferred as a storage follow-up, pending user review.

## How a request pins a snapshot

S0 reads the current pointer once and records `snapshot_id` in the trace; S1 and S3 read only that snapshot until the response is sent, so a publish or rollback during the request changes nothing for it. When retrieval is on and no snapshot is published, the request continues without knowledge, with warning `SNAPSHOT_MISSING`, retrieval state `failed` and status `degraded`.

## Retrieval off is structural

Every knowledge access (alias and name lookup, lexical and vector search, cards, knowledge-base images, caches derived from the knowledge base) goes through one knowledge gateway object. With `rag.enabled: false` it is never constructed and S1 and S3 are not in the execution graph, so no code path can reach the knowledge base, cached knowledge included (R-009, decision 12). The test is structural: run the off path with the knowledge base absent. A phrase such as "rag=false" inside the prompt has no effect, because the prompt is data.

## Agent rules for running anything

GPU jobs follow the VRAM rules ([PLATFORM_GPU_RULES](rules/PLATFORM_GPU_RULES.md), AGENTS.md section 8). A `kb sync` that calls a model, any job on the deployment and any paid cloud call run only with the user's go; a paid backend needs a call budget the user gave (`ask_user`), asked before its first call.
