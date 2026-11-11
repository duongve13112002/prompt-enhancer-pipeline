# Execution: commands, sync, snapshots, resume

How the pipeline runs end to end. Data shapes: [contracts](../contracts/README.md). Requirements: R-009, R-014, R-016, R-017, R-020. Decisions: 12, 17, 19, 20. Nothing here is implemented or measured; the design draws on [N124](note/N124-incremental-indexing.md), [N125](note/N125-snapshots-and-vector-deletion.md) and [N126](note/N126-content-addressed-caches.md).

## Where things run (R-020)

- **Everything works on the dev machine first**, end to end, on the example knowledge base and the agent-built scenarios: `kb sync`, admin profile registration, both tasks with retrieval on and off, validation and evaluation. A feature is done only when it runs there. Dev results show that code works, never quality ([dev-machine](dev-machine.md)).
- **The user runs the deployment.** The agent never logs in to it; it writes a run guide (commands, configuration overlay, expected outputs, how to check VRAM, resume and roll back) and reads the results the user pastes or places in shared storage ([serving](serving.md)).
- **Models** are reached over HTTP through the backend each `vlm.roles.<name>` binds to (R-018); request fields follow [VLM_CALL_RULES](rules/VLM_CALL_RULES.md).

## Commands

Each command refuses at start when a key it reads still holds a sentinel or is missing, naming the key, because a silent default hides a decision nobody made (R-015).

| Command | Does | Needs | Refuses |
| --- | --- | --- | --- |
| `kb status` | shows the current snapshot, pending changes and lint counts | `storage.kb_store` | nothing beyond a missing store |
| `kb sync [--plan] [--only <folder>]` | runs K0 to K5 and publishes a snapshot; `--plan` stops after the plan | `knowledge.*`, pinned models and revisions for the roles K1 to K4 use, `sync.*` | a plan over the mass-deletion guard without `--force`; unpinned models (`pin_required`); folders whose name and description counts differ (reported, not indexed) |
| `kb rollback <snapshot>` | moves the current pointer to a retained snapshot | the snapshot still retained | a snapshot already collected |
| `kb gc` | removes unreferenced artifacts and snapshots past retention | `sync.*` retention keys | artifacts a retained snapshot references |
| `kb audit` | samples card facts and stability labels for human checking (E16) | a published snapshot | nothing |
| `kb show <concept>` | prints a concept's card, aliases, folders and lint | a published snapshot | unknown concept (reported) |
| `profile register` | drafts the manifest and conflict list for a system prompt (P) | `profiles.root`, the `manifest_drafter` backend | nothing is activated until the admin confirms |
| `profile check` | runs conflict lint and golden tests; activates on success | a confirmed manifest and a decision for each conflict | activation while a conflict is `block_activation` or a golden test fails |
| `enhance once` | runs S0 to S6 for one request and prints the envelope | an active admin profile; with retrieval on, a published snapshot | a task the profile does not support (decision 5) |
| `enhance serve` | serves S0 to S6 over HTTP | as `enhance once`, plus `serving.*` | `deploy_required` values left in `serving.*` |
| `eval build` | has agents generate scenarios and answer keys (G) | a published snapshot, the `scenario_generator` backend, a call budget for paid backends (`ask_user`) | a paid backend without a budget |
| `eval run` | runs scenarios through the pipeline and scores them | built scenarios, an active profile | scenarios built from another snapshot unless named |
| `eval fit` | fits `fit_from_*` values on the `fit` split and writes an evidence report | results on the `fit` split from the deployment's models | fitting on the test split, or on dev-machine runs |

## The `kb sync` flow (R-014, decision 17)

1. **Scan** the whole source tree every time, because an incremental listing cannot see deleted sources ([N124](note/N124-incremental-indexing.md)). Size and modification time pick the files to hash; the hash decides.
2. **Diff** against the current snapshot: added, changed, moved, removed images and folders, and changed info files and `kb_overrides.yaml`.
3. **Plan:** print the work and the number of model calls per role. `--plan` stops here.
4. **Guard:** stop when the plan removes more than `sync.mass_deletion_guard.max_removed_fraction` (`design_required`) of the knowledge base, for example when the source drive is not mounted; `--force` overrides. A full scan that lists too little would otherwise delete everything ([N124](note/N124-incremental-indexing.md)).
5. **Execute** per artifact in the order K1 (image embeddings), K2 (first the info-file assessment and the naming of folders without a usable info file), K3, K4, K1 (concept-text embeddings), resumable; an undecodable file is recorded, skipped and reported.
6. **Build and validate** the new snapshot beside the current one (K5): referential integrity and sample queries.
7. **Publish** by moving the current pointer atomically; retain `sync.snapshots.keep` snapshots for rollback.

With `knowledge.on_source_removed: remove` (the default) a deleted source loses its knowledge at the next sync; until then the database and running requests are unaffected, because the request path reads only the snapshot (decision 17).

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
- **Resume:** progress lives in `sync_state`, written after each artifact's output (commit last). A rerun skips every artifact whose key exists and redoes the rest; a half-written artifact has no state row and is redone.

## Snapshots, publish, rollback, garbage collection

A snapshot is an immutable directory under `storage.kb_store` with a manifest: the tables (`folders`, `concepts`, `folder_concepts`, `images`, `folder_images`, `image_embeddings`, `concept_text_embeddings`, `observations`, `cards`, `aliases`), the serving index built from them, and copies of the knowledge-base images the `attach` channel may send. Publishing moves one pointer; `kb rollback` moves it back. Readers pinned to an older snapshot keep a consistent view while a new one is written ([N125](note/N125-snapshots-and-vector-deletion.md)). The index is rebuilt per snapshot rather than edited in place, because some approximate nearest-neighbour indexes cannot delete entries ([N125](note/N125-snapshots-and-vector-deletion.md)). Unreferenced artifacts are kept for `sync.gc.retention` before `kb gc` removes them, so restoring a file removed by mistake costs no model call.

## How a request pins a snapshot

S0 reads the current pointer once and records `snapshot_id` in the trace; S1 and S3 read only that snapshot until the response is sent, so a publish or rollback during the request changes nothing for it. When retrieval is on and no snapshot is published, the request continues without knowledge, with warning `SNAPSHOT_MISSING`, retrieval state `failed` and status `degraded`.

## Retrieval off is structural

Every knowledge access (alias and name lookup, lexical and vector search, cards, knowledge-base images, caches derived from the knowledge base) goes through one knowledge gateway object. With `rag.enabled: false` it is never constructed and S1 and S3 are not in the execution graph, so no code path can reach the knowledge base, cached knowledge included (R-009, decision 12). The test is structural: run the off path with the knowledge base absent. A phrase such as "rag=false" inside the prompt has no effect, because the prompt is data.

## Agent rules for running anything

GPU jobs follow the VRAM rules ([PLATFORM_GPU_RULES](rules/PLATFORM_GPU_RULES.md), AGENTS.md section 8). A `kb sync` that calls a model, any job on the deployment and any paid cloud call run only with the user's go; a paid backend needs a call budget the user gave (`ask_user`), asked before its first call.
