# Implementation notes

Decisions, deviations, assumptions, tradeoffs, rejected alternatives and risks, with links (AGENTS.md section 9). A proposal here is never an approval.

## 2026-11-11 repository seeding

- **Built from:** the user's proposal v1.1 and the PM's design v2.2 ([N000](note/N000-design-provenance.md)), the user's answers of 2026-11-11 (decisions 1 to 27), and the agent tooling, policies and note format of an earlier project of the user.
- **Tooling:** `tools/agent-workflow/` and `scripts/agy-runner/` were copied without code changes; only `.agents/project.json` and `tools/agent-workflow/doc_budgets.json` hold new values. Known leftovers that need code changes are tasks T-020 to T-022.
- **Rules:** `AGENTS.md`, the brief footer and the template were reworded per decision 24; section and footer-rule numbers were kept because skills and tools cite them. Mapping: [agents/rule-map.md](agents/rule-map.md).
- **Deviations from the PM's design v2.2, forced by decisions 18 and 19:** no transliteration tables or word lists (aliases come from a model, negation is judged by the verifier); Parquet and Arrow instead of an embedded SQL store; every hand-typed size or count replaced by a sentinel.
- **Assumptions:** a folder's images all show every concept the folder declares (the user's statement for one concept, extended to several); the same concept name after Unicode normalisation is the same concept (decision 10).
- **Rejected:** a tool-calling agent loop for the writer (unpredictable latency, weaker isolation of retrieval-off and of scope); placing the whole concept catalogue in the planner context ([N120](note/N120-long-lists-in-context.md)); a code skeleton (AGENTS.md forbids stubs and implementation before an explicit command).
- **Risks:** a wrong concept card affects every request that uses it (audit E16 is the gate); most research notes are agent-read; the workflow changes of decision 24 rest on vendor guidance and are unmeasured (T-023); GitHub facts behind the CI window are unverified ([N134](note/N134-github-visibility-and-actions.md)).
- **Not done:** no tool, hook or test was run in this repository beyond the read-only `setup_project.py check` at seeding (result in [tasks/STATUS.md](../tasks/STATUS.md)); nothing was benchmarked.

## 2026-11-16 K1 embeddings (T-008)

- **Design:**
  - K1 embeds new or changed images (decoded once, resized by T-009's `prepare_image`) and concept text through a named `vlm.backends` entry over HTTP. Rows are keyed by content hash plus embedder identity and revision.
  - Checkpoints are kept per input hash, so a resumed run makes no extra calls.
  - Every key K1 reads is validated before any read or request. `vision.format` must be a Pillow encoder with an `image/` media type, and the media type is read from Pillow's registry (`config.image_media_type`, shared with T-009). Every timer and backoff must convert to a finite float.
  - Retry backoff is doubled and capped after each attempt, with no power, so long retry counts cannot overflow.
  - Task file: [T-008](../tasks/active/T-008-k1-embed.md).
- **PM decisions pending user review:**
  - The embedder is a named `vlm.backends` entry, and there is no `embedding.devices` key.
  - Both the llama.cpp and the vLLM image adapters are built, and the request shape stays `design_required` until a live smoke.
  - Pixel size is stored before EXIF orientation.
  - The finite-float timer rule applies to every backend.
- **Integration:** the `config.py` import line was merged by hand onto the anchored layer. K1's helper `_commit` was renamed `_commit_record`, because the anchored-writes AST guard refuses that name, which belonged to a removed storage helper; behaviour is unchanged.
- **Risks:**
  - No live embedder has run yet (E5, E12).
  - ICO under 16 px gives an empty container (P2, inherited from T-009).
  - MPO is labelled `image/mpo` while its bytes are JPEG.

## 2026-11-16 storage: one anchored write layer (T-006 race follow-up, T-006-int)

- **Decision (user, decision 123):** the store is locked while a release deletes; a junction or link swapped into a store path between check and act must never redirect a write.
- **Design:** every create, write, rename, lock-file open and delete in `src/prompt_enhancer` goes through `storage/_anchored.py`, which opens each path component relative to its verified parent without following links (Windows `NtCreateFile` with a root handle and rename on the handle; POSIX `dir_fd` with `O_NOFOLLOW`); an AST test refuses by-name write forms in every module. Profile bundles (T-012) are anchored at the resolved project root; register's no-replace publish is a handle rename without replace on Windows and `linkat` on POSIX, with an anchored exclusive-create fallback. Task files: [T-006-race-followup](../tasks/archive/T-006-race-followup.md), [T-006-int](../tasks/archive/T-006-int.md).
- **PM decisions pending user review:** round 3 covered the whole class instead of accepting a P1 residual; a failed `release()` becomes a note on the caller's exception, or a `StorageWarning` when the work succeeded; profile folder names that fail `check_segment` (8.3 short names, non-NFC, control or forbidden characters) are refused.
- **Rejected:** `rename_entry` without replace for register's publish (its POSIX branch checks, then `renameat` overwrites a manifest created in between); anchoring at the bundle or `profiles.root` (the folders above would still be opened by name).
- **Risks:** POSIX branches are source-read only until a Linux CI run; NTFS rename durability rests on metadata journaling (no directory fsync on Windows); the AST guard misses some write forms no code uses today (listed in [tasks/STATUS.md](../tasks/STATUS.md)).

## 2026-11-19 command line P1 and P2 (T-016 round 1)

- **Built:** configuration and dispatch for every command, `enhance once` through the S0-S6 coordinator, attachment publication under `data_root/attachments/<trace_id>/` before delivery, expiry by `runtime.trace.retention_s`; P3 serve, P4 eval and P5 run guide are later rounds and refuse with exit 2. Task file: [T-016](../tasks/active/T-016-cli-run-guide.md) (deviations D1 to D7).
- **PM decisions, accepted by the user 2026-11-19 (decision 167):** a Claude reviewer instead of Codex (Codex out of quota); every path the new commands take from the command line (`--config`, `--overlay`, `--request`) refuses links, also inside the root; `serving.http.max_request_bytes` in the dev profile is a proxy from the retained-image bound, because `request.v0` has no length limit.
- **Follow-ups:** the shared loader still follows in-root links for `extends`, `overlay_of`, profile and schema paths (D6; move the no-link rule into the loader); no test covers a request file that grows during the bounded read; `cli_commands` imports the private `kb_audit._audit_snapshot`.

## 2026-11-19 `enhance serve` (T-016 P3)

- **Built:** standard-library HTTP serve of S0 to S6 with bounded admission (`_Admission`: `max_concurrency` running, `queue_limit` waiting, refusal 503 when both are full), one deadline per read and write, delivery acknowledged only after the flush and abandoned on every other path (`DeliveryTicket.abandon()`, `PipelineResponse.abandon()`), attachment download through the no-follow bounded reader, a cooperative stop that counts only requests whose head was read. Task file: [T-016](../tasks/active/T-016-cli-run-guide.md) (blocking operations B1 to B14, deviations D8 to D14, Configuration audit of the 10 `serving` leaves).
- **PM decisions, pending user review:** `PipelineResponse.abandon()` was added beside the granted `DeliveryTicket.abandon()` (the serve layer holds only the response; the retest judged it needed and safe); the dev profile leaves `max_concurrency`, `queue_limit` and `total_deadline_s` unresolved, so dev `enhance serve` refuses until they are set; the test fixture `pipeline_config` sets `runtime.request_deadline_s` 120 (test-only; no test asserts deadline behaviour through it). The PM fixed the retest's two P3 (R1 a cancel landing in the turn generation finished now abandons the response; R2 `_Admission` keeps the `CancelledError` of a waiter `leave` already popped).
- **Follow-ups:** `attachments._expiry` (the sweep) reads the index by name without a size bound and could reuse the bounded `_read_once`; the monitoring path is tested end to end only with a labelled double, because the shared profile has no calibrated verifier.

## 2026-11-19 declared stage revisions (KBS-S3-1)

- **Built:** every K0 to K5, model-call and `kb sync` key carries a declared revision instead of a hash of module source, so editing an unrelated module no longer re-embeds every image (smoke 3 finding). `tests/stage_guard.py` lists the output-shaping functions and constants of each stage and `tests/test_stage_revisions.py` pins a digest of their normalised syntax trees per revision (append-only rows) and fails on any function of a covered module that is neither pinned nor left out with a reason. The next `kb sync` recomputes everything once. Task file: [KBS-S3-1](../tasks/active/KBS-S3-1-stage-revisions.md).
- **Decisions:** the stage-revision design was accepted by the user (decision 167). `embedding.batch_size` stays out of the K1 key, as before; [K1](stages/K1-embed.md) says a changed value alone keeps every stored vector. The PM fixed the retest's two P2 at integration (decision 164): K1 batch membership (`_batch_members`, `_companion_lost`) is pinned in `k1_image`, and `K2_DRAW_REVISION` joins the `kb sync` identity.
- **Follow-ups:** the completeness test does not cover `model_transport`, `model_resources`, `model_probe`, `model_secrets`, `_os_support` or `storage.*` (timing, transport or file access only), so nothing forces a new content-shaping function there to be classified.

## 2026-11-19 PM decisions accepted (decision 170)

- The user accepted the pending PM decisions of K4-ALIASFAIL (run-level codes block), T-016 P3 and P4, the identical-publish skip of P3-SWEEP-1 and the `attach` notes in the writer brief (a later small task), on the standing condition that no change breaks the pipeline's intent (R-001, R-004, R-009, R-026): AGENTS.md section 10 and standing rule S49. The "pending user review" labels below are resolved by it.

## 2026-11-19 a failed alias call does not block the publish (K4-ALIASFAIL)

- **Built:** decision 165 in `kb_sync.sync_knowledge`: a concept whose K4 generation fails publishes without its generated aliases (author aliases stay; they need no model call), the failure is recorded as concept ids and closed codes (`alias_failures` in the result, `k4_failed` in `source_scan_summary`), the unchanged-snapshot branch does not short-circuit while failures are recorded, and the next sync re-calls only the failed keys; `KB_SYNC_REVISION` is `kb-sync-2`. Task file: [K4-ALIASFAIL](../tasks/active/K4-ALIASFAIL-alias-failure.md).
- **PM decision pending user review:** only per-concept codes publish (`SCHEMA_INVALID`, `INCOMPLETE_OR_REFUSED`, `MODEL_CALL_INVALID`, `MODEL_CALL_FAILED`, `HTTP_ERROR`, `INPUT_INVALID`); a run-level code (configuration, client state, capability, probe, budget) means no concept could succeed, so it still fails `k4: STAGE_FAILED` (`require_per_concept`). This is the conservative reading of "bỏ alias đó"; the alternative publishes on any code. The `kb-sync-2` pin row was updated in place because it was never released.
- **Follow-ups:** while one key keeps failing, every sync republishes an otherwise identical snapshot (consider reusing the previous one); coverage of the pinned reader and of every concept failing at once is thin.
- **Decision 171 retro review (Gemini, 2026-11-19):** `require_per_concept` checked `_closed_code(code)`, which maps an unknown or non-string code to the tolerated `MODEL_CALL_FAILED`; it now checks the raw code, so an unrecognised code blocks (fail closed). Unreachable today (`failure_code` returns only closed codes), so no outcome changes; `test_an_unrecognised_alias_code_blocks_the_publish`, old check as mutant: 6 of 6 cases fail. Its second review (PASS) noted a P3 that predates this: `k4_aliases.py` catches `TypeError` around internal steps (`_record_revisions`, `_generation_bundle`, the commit) and `failure_code` turns it into `SCHEMA_INVALID`, so since decision 165 a programming error in K4 publishes without aliases instead of failing the stage. Follow-up: narrow that `try` to the model call and reply parsing.

## 2026-11-19 evaluation runner (T-016 P4)

- **Built:** `eval run`, `eval import` and the blinded judge pack (`eval_set.py`, `eval_run.py`, `eval_judge.py`): a run over the draft set with `--variants record|both` (required), bounded concurrency `evaluation.run.max_concurrency`, a run lock held until the summary and the pack are written, resumable units (`not_run` with a reason, retried on resume), sidecars under the data store, a judge pack whose outputs carry blind labels and a scores import validated against `eval_judge_scores.v0`. Task file: [T-016 P4](../tasks/active/T-016-P4-eval-runner.md).
- **Decisions (accepted by the user, decision 170):** `--variants` required; a named snapshot must equal CURRENT; single-output pack items cannot hide their variant (documented limit); `prompts/eval_rubric_v0.txt` is a neutral draft never sent to a pipeline model. Decision 172: the T-017 pilot runs `--variants both`, scored by Claude and Gemini.
- **PM integration:** test-only trace headroom in `tests/eval_fixtures.py`, a test that the lock is held while the summary and pack are written (mutant O1 killed), unused constants removed; the Gemini review (decision 171) found no defect in these three. The runner's streaming reader made `read_source_file` delegate to the new `iter_source_file`; both are pinned in `k0` and `k1_image`, so the revisions moved to `k0-scan-v6` and `k1-image-4` (the next `kb sync` recomputes K0 and the image embeddings once; no deployed knowledge base exists yet). The rejected alternative, a second copy of the reader loop, would duplicate the link, size and change checks.

## 2026-11-19 P3 follow-ups sweep (P3-SWEEP-1)

- **Built:** a damaged K5 index file is its own request diagnostic (`INDEX_DAMAGE_MESSAGE`, fixed text, no path); the attachment sweep reads its index through the bounded reader; a K4 retry that fails identically skips the publish (`status: unchanged`, `publish_skipped: identical`, `kb-sync-3`; accepted, decision 170); a per-spawn guard for shard variables in tooling tests; stable `test_k1_config` ids; a deterministic over-cap test. Task file: [P3-SWEEP-1](../tasks/active/P3-SWEEP-1-follow-ups.md).
- **PM integration:** the index message no longer advises `kb sync`, which cannot repair a damaged current snapshot; `docs/execution.md` names the next writer session as the one that removes a skipped build directory.
- **Follow-ups:** sync recovery from a damaged current snapshot (and the same false hint in `RETENTION_MESSAGE`); `retain_images` after the identity check; the over-cap test's sensitivity; the spawn guard's missed forms; failed concept ids in the identity check (task file, section "PM integration").

## 2026-11-19 one note per attached image in the writer brief (ATTACH-NOTES)

- **Built (decision 170 item 4):** `writer.render_brief` renders `knowledge.attachments`, one entry per `attach` image in S6 order (`position`, `image_id`, `bound_entity`, `facets`, decoded `note`); `assembly.attached_note` is the single decoder and checker of an image note, used by S3 validation and S4. Task file: [ATTACH-NOTES](../tasks/active/ATTACH-NOTES-writer-brief.md).
- **Superseded by decision 177:** with `renderer.accepts_reference_images: false` no `attachments` key is rendered; the user chose a refusal at configuration load instead (task KB-EXTERNAL).
- **Decided (decision 176):** source and reference images are numbered as two groups with their own syntax templates, and `edit` takes n source images (task MULTI-SOURCE). Still open: a `brief_v1` template line naming `attachments` (a new prompt revision judged by an eval, not an in-place edit of `brief_v0`).

## 2026-11-19 link rule and P3-SWEEP-1 follow-ups (P3-SWEEP-2)

- **Built (decision 174):** a path the project writes or owns (models, cache, stores, data, profiles, schemas, verifier) refuses a link or another spelling at any component (`config.LinkedPath`); `knowledge.root` follows a link whose resolved target must stay inside the project root, at load and in every stage (K0 to K4). K4's alias `try` holds only the model call and its parse (decision 165); `retain_images` runs after the identity check; the K4 failure summary keeps `concept_ids_hash`; `RETENTION_MESSAGE` no longer advises `kb sync`. Task file: [P3-SWEEP-2](../tasks/active/P3-SWEEP-2-follow-ups.md).
- **Fix round:** K4 resolved the knowledge root strictly (Claude Opus P1); a `kb sync` flow test through a real link now covers every stage. To make that flow work, `kb_sync.sync_knowledge` passes the resolved root to K1 and K2 (`kb-sync-5`: the first sync after the upgrade re-plans once). PM decision pending user review.
- **Kept as before (PM decision pending user review):** no recovery path from a damaged current snapshot (false hint removed only); K2 naming and K3 keep one `try` per item (9 accepted tests pin them).
- **Follow-ups:** sync recovery from a damaged snapshot; a POSIX link target holding `..` after an earlier link (validated lexically, read physically; predates this change); the volume mount point wording on Windows.

## 2026-11-19 purge rule for pytest data in worker worktrees (FOLLOWUPS-1)

- **Built:** `purge_tmp.py archive-remove` treats a protected name (never `.secrets`, any case) as test data only below `<worktree>/.tmp/<dir>/` inside a directory named exactly as pytest's `_mk_tmp` names a test function defined in that worktree's own test files (non-word characters to `_`, cut at 30, ASCII counter); links are never followed; the other purge commands are unchanged. Also: the run-guide test checks long options against the real parser, `K3-cards.md` and `execution.md` lint sentences corrected, four configuration headers updated. Task file: [FOLLOWUPS-1](../tasks/active/FOLLOWUPS-1-small-fixes.md).

## 2026-11-19 external knowledge root and the attach refusal (KB-EXTERNAL)

- **Built (decision 175):** `knowledge.root` may lie outside the project. One physical resolver, `config.resolve_knowledge_root`, is used by every stage, `kb_sync` and `dev_compare`. It refuses the project root or a container of it, and `models/`, `.secrets/` (compared by name, never resolved, also for a `\\?\` spelling), `.git`, `.venv*`, `data_root` and `kb_store`, by equality or containment either way. "Inside the project" in decision 175 is read as "inside a protected directory", so `database/` stays valid (PM-approved Step 0). `KB_SYNC_REVISION` is `kb-sync-6` (the first sync after the upgrade re-plans once); every `activation.json` is stale and needs `profile check` again. Task file: [KB-EXTERNAL](../tasks/active/KB-EXTERNAL-knowledge-root.md).
- **Built (decisions 177 and 184):** `profiles._view` refuses `renderer.accepts_reference_images: false` with `edit.attach_kb_reference_images: true` for a profile that supports `edit`; a generate-only profile is not refused (decision 184). The refusal is `profiles.AttachmentRendererMismatch`, a fixed text naming both keys and values and no path, passed through by `intake._refuse`, so `enhance once`, `enhance serve` and `eval run` show it like `profile check`. The writer's degraded mode became a defensive error.
- **Docs:** retrieval off skips the root for `kb sync` and the request commands only (other commands still resolve it, metadata only, as before); FAT, exFAT and network shares may be refused (`MissingFileID`), not verified; `kb sync --plan` on the real drive is a deployment check.
- **PM decision pending user review:** `tests/intake_fixtures.activate` gives an image-accepting renderer when the configuration attaches images (a test helper default).
- **Follow-ups:** other `ConfigurationError` texts stay redacted at request time (`_require_keys`, `_config_fingerprint`, the addendum prompt, the profile id); `edit.attach_kb_reference_images` is not in the activation fingerprint; `enhance serve` answers 400 with one text for any `ConfigurationError` raised after intake; a `.secrets` that is itself a link is not recognised.

## 2026-11-19 retry with another seed (K3-RETRY, decisions 183 and 188)

- **Built:** a K3 card reply, a K2 observer reply or a plain `image` probe challenge that is refused (a parse or schema refusal of a reply that arrived, client `SCHEMA_INVALID`, the probe's `PROBE_MALFORMED`) is sent again, identical except `sampling.seed`, up to `cards.reply_attempts`, `observe.reply_attempts` and `vlm.backends.<name>.probe.image_attempts`; every example profile holds `fit_from_bench` until the seed experiment fits them. Attempt 0 is the profile's request unchanged; attempt k sends `(role sampling.seed + k) mod 2^63`. A count above 1 needs an integer `sampling.seed` and a backend that maps `seed` (`model_mapping.SAMPLING`), checked on the backend the call or image probe really uses (`config.image_probe_backend`); the probe allowance is checked at load from the project's probe bundle. `HTTP_ERROR`, `INCOMPLETE_OR_REFUSED`, budget, configuration, cancellation and storage errors are never retried; exhaustion fails the stage and nothing is published. Each attempt records attempt, seed, closed code and reply digest; identities use the configured role, so a card stored on attempt k is not asked again. Shared loop: `reply_retry.call_until_valid`. Revisions `k3-card-3`, `k2-observe-3`, `model-call-3`. Task file: [K3-RETRY](../tasks/active/K3-RETRY-seed-retry.md).
- **Rejected:** feeding the validator message back to the model (a design change nobody approved; R-026); stripping code fences from probe replies (weakens what the probe proves); a separate seed key; resuming attempts across runs.
- **Evidence note:** a later run of the same item replaces its failure row and repeats the same seeds, and `kb gc` removes failure rows after `sync.gc.retention`, so the seed experiment reads the rows after each run.
- **Follow-ups:** `text` and `multiple_images` probes can be fenced the same way (decision 188 names only `image`); the attempts keys have no upper bound; probe `body_hashes` cover attempt 0 only; the per-role allowance arithmetic leaves out other roles on the same backend (runtime `PROBE_INCOMPLETE`).

## 2026-11-19 golden-case causes of the T-017 `profile check` (T017-FIX, decisions 190 to 193)

- **Built (decision 190):** every `openai_compatible` chat backend states `vlm.backends.<name>.addendum_layout`: `separate_system_message` (two system messages, as before) or `joined_system_message` (one system message: the admin bytes unchanged and first, then `addendum_separator`, then the runtime addendum), because the Qwen3.5 chat template refuses a system message that is not first (HTTP 500 in J4a2). Cloud and chat-less backends refuse both keys. The layout is read from the backend the request actually goes to, so a budget or image fallback uses its own. `effective_system_hash` of the system text sent goes to the call record, `writer_result.v1` and the trace; the S5 repair uses the same writer. Dev example: joined with "\n\n"; base and overlay examples: `deploy_required`.
- **Built (decision 193):** `addendum_separator: none` (protocol-bound, `config.NO_SEPARATOR`) is accepted only with the separate layout, so a child of the dev example can switch layouts; any other separator under separate, and `none` under joined, are refused at load and again before any HTTP request.
- **Built:** generate requests send `planner_reply_generate.v0` (the shared schema with `edit_kind` null and `maxItems: 0` on the edit arrays); edit keeps `planner_reply.v0`. The planner invariant already refused those fields for generate, so nothing accepted before is refused now (R-001).
- **Built (decision 192):** the planner keeps the model-call code as `plan_result.v1` `error.cause`; `PipelineResponse.diagnostics`, `GoldenAdapter.run_case` (`s0-s6.v2`), `golden_results.json` and eval markers keep code-only diagnostics (R-026). `docs/stages/S2-planner.md` now states the code's behaviour: a failed call gives a `failed` plan with its cause, an invalid reply is retried once then gives a `degraded` plan, S4 does not run, and S5's `validation.fallbacks` may still give an output.
- **Schemas:** `plan_result`, `writer_result` and `validation_result` moved to v1; `enhancer_config.v0` took the new keys in place. **PM decision pending user review:** draft configuration schemas take additive keys in place, stage and stored-artifact schemas get a new version (`contracts/README.md`).
- **Integration with K3-RETRY:** both bumped the model-call revision; the combined code is `model-call-4` with a new pin row, K3-RETRY's `model-call-3` row kept. `model_mapping.system_messages` imports from `config` lazily to break the cycle K3-RETRY's `config -> model_mapping` import created. Task file: [T017-FIX](../tasks/active/T017-FIX-golden-causes.md).
- **Rejected:** deriving the generate schema at run time; changing what `run` returns for golden diagnostics; ignoring a separator under the separate layout (an ignored setting); dropping the plain `image` probe requirement (decision 191 keeps it).
- **Not verified:** whether the Qwen3.5 template accepts the joined message and whether llama.cpp's grammar enforces `maxItems: 0` and `{"type": "null"}`: the live J4a2 re-run decides.

## 2026-11-19 shard-independent K5 scores (K5-TIE)

- **Root cause (experiment):** `ServingIndex.search` scored each shard with a BLAS matrix-vector product, whose bits depend on the shard shape (`-2.2e-17` in one shard and `0.0` in another for the same vector), so ties among equal vectors broke on float noise instead of the primary key and the ranking depended on `index.build.block_rows`. Found by the full suite (`tests/test_k5_properties.py`).
- **Built:** the score is an elementwise product and a row sum over `np.ascontiguousarray(matrix, dtype=SCORE_DTYPE)` (no copy on today's path), for both metrics; nothing rounded or zeroed; `_matrix` and the stored index unchanged, so no revision change. Tests: the failing example, a bitwise property over shard size, position and count (float vectors, dimension 1 to 300), a memory-layout test, a differential test against the old product within `2*dim*eps*sum|ab|`, tiny-score cases for both metrics. Cost: scoring alone about 13 times slower on 8192x1024, the whole shard step about 2% (`_matrix` dominates); CPU smoke, not a timing claim. Task file: [K5-TIE](../tasks/active/K5-TIE-shard-scores.md).
- **Not verified:** bitwise equality across numpy builds or machines (only within one build).

## 2026-11-20 seeded planner retry and writer repair, shortlist-bounded planner schema (T017-FIX2)

- **Root causes:** found by the read-only report T017-RCA3 on run J4a3 and confirmed against the code.
  - The planner's one retry (decision 192) resent a byte-identical request, so under a fixed `sampling.seed` it bought nothing (G4).
  - With an empty shortlist the reply schema left positions open, and the dev model invented `knowledge_needs[0].candidate_position: 0` (G3).
- **Built, retries:** the planner retry and S5 writer repair round k change only `sampling.seed`, to `attempt_seed(base, k)`, the K2/K3 pattern of decisions 183 and 188.
  - Attempt 0 is unchanged.
  - Nothing about a refused reply goes back (R-026).
  - An unseeded role stays unseeded.
  - Every attempt's seed is recorded.
- **Built, bounded schema:** the planner sends a per-request schema derived from the registered one.
  - It only adds upper bounds to shortlist, variant-group and variant positions, plus `maxItems: 0` on arrays that can hold no valid item.
  - The reply is validated client-side against both schemas.
  - The record keeps the base `schema_id` plus `sent_schema_hash` when the schema is in the body (`json_schema` on `openai_compatible` and `anthropic`) or `client_schema_hash` when only the client checks it.
- **Revision:** `model-call-5`, so the K2, K3 and K4 artifacts recompute once at the next `kb sync`.
- **PM decisions pending user review:**
  - The planner retry is refused (`PLANNER_CALL_FAILED`, cause `CONFIGURATION_INVALID`) when the backend drops the seed, because it would repeat attempt 0 byte for byte.
  - The writer repair is never refused for a dropped seed, because its brief already differs (draft and failures); the attempt records the seed as null with the `PARAM_UNSUPPORTED` warning.
  - `drops_seed` lives in `planner.py`, so that no K2/K3 pin bump is needed.
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS WITH NITS (F1, the writer refusal, and N1 to N4); one fix round; Gemini diff-only retest PASS.
- **Rejected:**
  - repairing a reply by dropping out-of-range knowledge needs, which would break "planner replies are never repaired";
  - a prompt line about empty shortlists, which is weak on dev models and changes a runtime prompt.
- **Not verified:** whether llama.cpp's grammar honours `maximum` inside `anyOf` and `maxItems: 0` (client-side validation enforces both); live behaviour until the next golden run. Task file: [T017-FIX2](../tasks/active/T017-FIX2-retry-seed-schema-bound.md).

## 2026-11-20 admin output schema, golden retrieval switch, envelope v1 (T017-FIX3, decisions 194 and 197 to 200)

- **Built:**
  - An admin bundle may ship `output_schema.json`; its `$id` must equal the manifest's `output_schema_id`. It is checked at every profile load, may not shadow a project contract, and its SHA-256 enters `activation.json`.
  - Every writer attempt and S5 repair sends it as `json_schema` and validates the reply against it. `none` sends no schema. The system prompt bytes and the message order are unchanged (R-004).
  - `golden_case.v1` states `request.retrieval`. A `disabled` case runs on a retrieval-off pipeline: no store, no S1, S3 or embedder call (R-009). An `enabled` case under a retrieval-off configuration is refused before any case runs. v0 cases are refused with a message naming v1.
  - `response_envelope.v1` and `intake_result.v1` carry a required `retrieval.error` (null or a closed code) and the `not_reached` state. `degraded` never passes a grounding case.
- **Revision:** `model-call-6`, shared with CLOUD-1 under one bump at integration, so K2, K3 and K4 recompute once at the next `kb sync`.
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS (3 own mutants; one equivalent). No fix round.
- **Open, P3:** a bundle schema's unresolvable `$ref` passes the load check and raises at validation time (`_WrappedReferencingError` is not caught as `SCHEMA_INVALID`); `profile check` still fails the profile through its golden case. Three refusal branches of `load_output_schema` have no test. Task file: [T017-FIX3](../tasks/active/T017-FIX3-output-schema-golden-envelope.md).

## 2026-11-20 Anthropic backend ready for Claude Haiku 5.5 (CLOUD-1, decisions 195 and 202 to 205)

- **Built:**
  - `cloud_claude` roles send `output_config = {effort, format: json_schema}`. The sent schema is the full one with only unsupported keywords removed, which can only widen it; a schema that cannot be sent (`not`, `oneOf`, non-`false` `additionalProperties`, external `$ref`, recursion) is refused at start. Replies are still validated against the full schema client-side. `sent_schema_hash` covers the posted schema, `client_schema_hash` the full one.
  - Haiku 5.5 settings the API answers with 400 (`thinking.budget_tokens`; `thinking: disabled` above effort `high`) are refused at load and again at request build, from a protocol-bound model-id table that cites its source.
  - On a protocol with no seed field, a retry goes out as a fresh sample (`seed: null`, `seed_field: none_in_protocol`); a seeded backend that drops the seed is still refused (decision 205).
  - `on_budget_exhausted: stop` ends the run with `BUDGET_EXHAUSTED` and never falls back to the local backend. `call_budget` is counted per run object and per HTTP attempt, in memory; the PM tracks the 3000-request total (decision 203).
  - The key is sent as `x-api-key`, no longer `Authorization: Bearer` (Bearer is for OAuth tokens).
- **Revisions:** `model-call-6` (shared with T017-FIX3), `k2-observe-4`, `k3-card-4`.
- **PM decision pending user review:** numeric `temperature`, `top_p` and `top_k` in a Haiku role stay warned and unsent, because `vlm_role.v0` accepts `none` only for `seed` and `stop`; the docs say to omit them.
- **Primary docs re-read by the PM (2026-11-20, platform.claude.com structured-outputs, extended-thinking, effort, Haiku 5.5 overview):** `budget_tokens` returns 400 on Haiku 5.5; `disabled` only at effort `high` or below; default effort `medium`; internal `$ref`/`$defs` supported; `anyOf` beside `type` or `properties` in one object is rejected (only `enhancer_config.v0` has that shape, and it is never sent).
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS (3 own mutants killed). No fix round. **Not verified until the live probe (decision 207):** whether the API accepts `pattern`, `$id`, `$schema` and `$comment` in a sent schema. Task file: [CLOUD-1](../tasks/active/CLOUD-1-anthropic-backend.md).
- **Live probe result (T017-HAIKU-FIX1, PM fix, experiment with `claude-haiku-5-5`):** the first production-path call with `info_assessment.v0` got HTTP 400 "For 'object' type, property '$comment' is not supported". A keyword matrix, one request per keyword the docs leave unclassified among those the 50 project schemas use, gave 200 for `$schema`, `$id`, `title`, `description` on an object and `$comment`, `title`, `description`, `contentEncoding` on a string, and 400 for `minProperties` on an object. Both rejected keywords are now removed from the sent copy (`$comment` is an annotation; `minProperties` only loosens); a test sends every contract schema through the converter against an independent list. `model-call-7`, so K2, K3 and K4 recompute once more at the next `kb sync`. After the fix the same call returned a schema-valid reply. Gemini Flash diff-only PASS (decision 171). 15 of the 3000 Haiku requests used.

## 2026-11-20 multi-source contracts and prompt drafts (MS-1, decisions 176 to 182 and 196)

- **Built:** schemas `request.v1`, `intake_result.v2`, `planner_reply.v1`, `intent_ir.v1` and `plan_result.v2`, with valid, invalid and code-refused fixtures; prompt drafts `planner_v2`, `brief_v1` and `manifest_drafter_v1`, marked "draft, not selected" and named by nothing under `src/` or `configs/`; "designed, not yet implemented" paragraphs in S0, S2, S4 and S6. No runtime code; the single-image path is unchanged.
- **Versions:** T017-FIX3 owns v1 of the intake result, envelope and golden case, so multi-source uses `intake_result.v2`, `response_envelope.v2` (MS-5) and `golden_case.v2` (MS-6).
- **Left to code (MS-3, MS-4):** an index above n, ascending order and the n of 2 or more rule. **Left to MS-2:** `manifest_drafter_v1` writes `null` for a renderer value the system prompt does not state; `profile_manifest.v1` pins that.
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS. The 15 `intake_result.v2` cases that waited for `response_envelope.v1` ran at integration (97 passed). Task file: [MS-1](../tasks/active/MS-1-contracts-docs.md).

## 2026-11-20 manifest v1 and config v2 loader (MS-2, decisions 179 and 210)

- **Built:** config `schema_version: 3` (`enhancer_config.v2`): `vlm.backends.<b>.image.max_images_per_call`, `edit.max_source_images` and a planner and writer `multi_source` block are required; a limit is a positive integer, a sentinel or a missing key is refused at start, nothing is truncated or defaulted; a backend limit below the cap is refused; `request_overrides.allowed_keys` may not name a limit. Manifest `schema_version: 2` (`profile_manifest.v1`) adds the renderer block (`max_source_images`, source and reference syntaxes with exactly one `{n}` each, no other brace). The loader dispatches on the version; a child config below its base takes the base's version.
- **Old inputs:** version 1 and 2 configs load as before for non-request commands; request commands refuse them with one message naming version 3. A v0 manifest loads as before; it is refused only when it supports `edit` and the paired config's cap is 2 or more. One-image requests are byte-identical (pinned by SHA-256).
- **Syntax overlap:** two renderings overlap when equal or one is a substring of the other (within a group for different numbers, or across groups). The check is decided from prefix, suffix and digit structure, never by rendering, so its cost does not grow with the cap (fix round 1; a 600-example differential test against the brute-force rendering). No numeric ceiling on the cap was invented.
- **Deviation, accepted for now (Q1, decision 213):** the refusal of a v0 manifest with attachments on moves to MS-5, where the multi-image request path lands. The PM checked it harmless before MS-5: MS-2 left `writer.py`, `pipeline.py` and `planner.py` unchanged and only load-time validation reads an image syntax.
- **Deployment note:** a request command reads `prompts/<id>.txt` of the two `multi_source` prompt ids at load (inside the project root); the per-request re-check does not.
- **Reviews (MS-2):** Gemini Flash PASS (concern: overlap cost); Claude Opus PASS with two P2 (v0 generate-only refused; unbounded overlap check on every request) and four P3; one fix round; Gemini diff-only PASS. Task file: [MS-2](../tasks/active/MS-2-manifest-config-loader.md).

## 2026-11-20 planner reply as a forced non-strict tool on Anthropic (CLOUD-2)

- **Cause:** on Claude Haiku 5.5 the shortlist-bounded planner schema as `output_config.format` got HTTP 400 "The compiled grammar is too large"; the limit is cumulative (T-017 H1 bisect), and the documented explicit limits apply only to `output_config.format` and `strict: true` tools.
- **Built:** `output.transport: tool_forced` (planner only, Anthropic only, configuration version 3): one non-strict tool with the unstripped per-request schema as `input_schema`, forced by `tool_choice`, no `output_config.format`; the reply is the single `tool_use` input, read after a strict body parse (duplicate keys and non-finite numbers refused) and validated client-side against the per-request schema; zero or several tool blocks, a non-object input, `max_tokens` or a refusal take the existing failure and retry codes; text beside the block is ignored (R-026). The capability probe sends the forced tool with the real schema for a `tool_forced` role and posts the real schema once for every Anthropic `json_schema` role, so a schema the backend refuses stops `profile check` at start; the load-time `probe.max_requests` floor counts it and names the needed number.
- **Decided (accepted by the user, decision 215):** `tool_forced` is the only new transport; a guarded schema never changes, so the value comes through the new `vlm_role.v1` composed by configuration version 3, and a file below version 3 that names it is refused; a planner that could fall back to a local backend (budget or image fallback) cannot select it.
- **Rejected:** no format with client validation (1 of 4 live replies failed the schema); assistant prefill (HTTP 400 on Haiku 5.5); a `strict: true` tool (the same grammar 400); editing `vlm_role.v0` or `enhancer_config.v0` in place.
- **Evidence:** 12 live Haiku requests (task file table): forced non-strict tool 5 of 5 HTTP 200 and schema-valid, the edit case with an image included. Quality on the tool path is not measured yet (the T-017 H1 rerun).
- **Revisions:** `model-call-8`; K2 naming, K2 observe, K3, K4 and the `kb sync` identity recompute once at the next `kb sync`. The local llama.cpp request bytes are unchanged (pinned).
- **Reviews:** Gemini Flash PASS; Claude Opus FAIL on one P1 (the guarded schemas) with one P2 and four P3; one fix round; Gemini diff-only PASS; the PM ran `test_ms2_config_v2.py` and `test_field_contracts.py` (644 passed). Task file: [CLOUD-2](../tasks/active/CLOUD-2-planner-transport.md).

## 2026-11-20 the Anthropic writer with the admin bundle schema (T017-FIX4)

- **Cause:** a pre-CLOUD-1 guard in `writer.prepare_write` refused every non-local writer with a structured format, so the Haiku pilot (writer `from_profile_manifest`, resolved to the bundle's `output_schema.json`) stopped every golden case before the writer (T-017 H1-r2); the CLOUD-1 class sweep had missed it. Of the other "not verified" guards, none was stale (task file table).
- **Built:** an Anthropic writer may send its resolved schema; it goes through `anthropic_output_schema` (the CLOUD-1 rules) at profile load (`profiles._view`, shared by `profile check` and `profile resolve`; `PROFILE_OUTPUT_SCHEMA_INVALID` naming `output_schema.json`) and again in `prepare_write`. Gemini and any other non-local provider stay refused. `golden_results.json` gains `error_message` for a `ConfigurationError` only, cut at `GOLDEN_ERROR_MESSAGE_CHARS` (1000, safety-bound); a `ModelCallError` or other exception keeps class and code, since its text may hold reply text (R-026).
- **Decided:** no probe change. The writer's probe runs at its first call with the resolved role and posts the stripped bundle schema, so a schema only the server refuses (for example the grammar limit) fails each case there, after the planner, before any real writer request (P3 in both reviews).
- **Effect:** `check_code_sha256` changes, so every activation needs a new `profile check`. `profile register` reads no bundle schema.
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS with two P3; no fix round. Task file: [T017-FIX4](../tasks/active/T017-FIX4.md).

## 2026-11-20 request and intake for several source images (MS-3, decisions 179 and 214)

- **Built:** S0 dispatches `request.v0` (one `source_image`) and `request.v1` (`source_images`); the structural rules live once in `request_shape.py`, used by `cli_input.parse_request` (file and HTTP) and `intake.validate_request`, and run before the schema so both versions give the same codes. New code `SOURCE_IMAGES_CONFLICT` (both fields); `SOURCE_IMAGES_TOO_MANY` and `SOURCE_IMAGE_DUPLICATE` as named by MS-1.
- **Limits:** the cap is `min(edit.max_source_images, renderer.max_source_images)`, 1 for a version-1 manifest (protocol-bound: it states no limit); the count is refused before any decoding and duplicates before any Pillow call; nothing is truncated.
- **Boundary until MS-4 and MS-5:** 2 or more sources build `intake_result.v2` in request order, then `Pipeline` refuses with `MULTI_SOURCE_UNAVAILABLE` before S1 (a failed `response_envelope.v1`, `source_binding: null`, no model call). A v2 payload has no `source_image` key, and `build_envelope` raises for a non-refused one, so no stage can silently use image 1 (R-001).
- **Decided (PM, Step 0 approval):** a one-image request (v0, or v1 with one item) keeps today's internal payload, so planner and writer are untouched; only a refused `request.v1` names the image index; `request.v0` refusal texts stay byte for byte. The "list shape for every request" lines of the design and docs were corrected; the `intake_result.v2` schema comment stays (schemas are never edited).
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS with one P3; no fix round. Task file: [MS-3](../tasks/active/MS-3-request-intake.md).

## 2026-11-20 S5 text checks on the reply text, one forced tool call, S5 diagnostics (T017-FIX5)

- **Cause (T-017 H1-r3):** a schema-bound writer reply reached S5 decoded, so the text-property checks `minified_json` and `single_line` could never pass (`profile_validators.py`), whatever the model wrote; the pilot's `single_line_minified_json` (decision 180) with its bundle schema (decisions 194, 206) failed every case. The local writer path had the same defect.
- **Built:** the client keeps the exact reply text on the decoded writer value (`model_json.JsonObject`/`JsonArray`, plain `dict`/`list` for every other use; only the writer role gets one); `json`, `minified_json` and `single_line` are judged on that text, every structure check on the decoded value; a value that no longer decodes from its text gets no text (the check fails, never a re-serialization); a repair is judged on its own text. The forced planner tool sends `disable_parallel_tool_use: true` (Anthropic: with `tool_choice` type `tool` it "means Claude calls exactly one tool"). Golden diagnostics list the S5 repair call and `validation.failed_items` (ids and closed codes only, R-026).
- **Decided (accepted by the user, decision 216):** text-property checks of an admin validator read the exact reply text, structure checks the decoded value.
- **Not changed (B2):** Haiku sometimes adds an undeclared top-level key to the tool reply (`scene_attributes`, `knowledge_needs_dummy`); no structural cause was found in the schema, prompt or tool, and identical requests succeed on a second draw, so the client still refuses such replies (no tolerance added; that would be a product decision).
- **Revisions:** `model-call-9` (K2 naming, K2 observe, K3, K4 and the `kb sync` identity recompute once at the next `kb sync`); `GoldenAdapter.identity` `s0-s6.v4`; `check_code_sha256` changes, so every profile needs a new `profile check`.
- **Open (P3):** the `minimal_output_valid_for_contract` fallback builds a plain dict, so it cannot pass a text check under a schema-bound profile (unchanged verdict); `single_line` on a decoded string root still judges the decoded string.
- **Reviews:** Gemini Flash PASS; Claude Sonnet PASS with three P3; no fix round. Task file: [T017-FIX5](../tasks/active/T017-FIX5.md).
