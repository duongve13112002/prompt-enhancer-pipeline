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
