# S6 Envelope: status, output, attachments, trace

**Purpose.** Return one response with an honest status, the output exactly as the admin profile's contract defines it, and everything else outside the output (R-024).

**Input and output.** The checked output and status from S5, the Knowledge Pack's `attach` images, the source binding, the retrieval state, then the response envelope ([contracts](../../contracts/README.md)) and one `request_traces` row.

**Method.**
- **Envelope fields:** `status`; `output` (verbatim per the profile contract); `attachments` (per image `image_id`, role `knowledge_reference`, the entity it binds to, the facets to use; images are copies from the snapshot, never from the source tree); `source_binding` (the source image's `image_id`); `retrieval` with `state` and the pinned snapshot; top-level `warnings`; `trace_id`.
- **Warnings** use the `response_envelope.v0` enum, including `VERIFIER_UNCALIBRATED` for permitted inline verification without matching evidence. Trace metadata uses deduplicated warning codes and keeps span-bearing warnings in their stage records. The output remains the profile payload.
- **Status** comes from S5, or `needs_clarification` from S2 when `clarification.mode` is `ask` and an edit target is ambiguous (with the candidate targets) or `resolution.on_family_match` is `ask` and a mention is a family match (with the candidate concepts). With `best_effort` (the example value, decision 68) no request ends in `needs_clarification`: the most plausible reading is written with status `degraded` and the reason in the trace.
- **Retrieval state [P]:** `disabled` when `rag.enabled` is false; `skipped_no_knowledge_needed` when the planner lists no knowledge need; `no_match` when no mention resolved to a concept, or concepts resolved but no fact passed the filters; `retrieved` when at least one fact or image was assembled (definitions: [contracts](../../contracts/README.md#enums)); `failed` when a knowledge access failed, the snapshot was missing included.
- **Trace** records the configuration hash, profile and addendum versions, `snapshot_id`, shortlist, Intent IR, resolution decisions, Knowledge Pack, executed brief, ledger verdicts, repairs and timings. It records both configured role model IDs even when a role is skipped or falls back. Call records identify the actual routes.

**Status precedence.** A fatal deadline or S0 refusal fails with null output. A valid ask-mode clarification returns null without S4/S5 calls. Unusable output fails. Usable output remains degraded after upstream degradation, failed retrieval, best-effort ambiguity or S5 fallback. Otherwise S5's checked first draft succeeds, and a checked repair is `repaired`.

**Library lifecycle.** `Pipeline` exposes `enhance`, response `after_delivery`, and `close` or async context management. It reserves trace capacity before generation. `runtime.trace.workers` bounds executor workers for trace writes and maintenance. The repository serializes segment appends under its lock. A selected monitor runs only after acknowledgement of the eligible checked output, makes one calibrated verifier call and leaves the delivered envelope unchanged. Closing joins active requests and all delivery cleanup, including capacity-skipped and unacknowledged tickets. `GoldenAdapter` runs the same coordinator with the confirmed view and snapshotted writer brief. T-016 owns transport delivery hooks.

**Retention and attachments.** Enhancement requests require `edit.attach_kb_reference_images: false` until the KB publisher retains hash-verified image copies in snapshots. The attachment path requires a successful hold before releasing a request lease. Trace and hold expiry must be representable before persistence. Maintenance indexes trace history once per lifecycle and removes expired segments through anchored storage. Closed services process expired rows on restart. A trace drain timeout is diagnostic and in-flight OS writes are joined.

**Decisions and configuration keys.** Positive finite `runtime.request_deadline_s`, `runtime.trace.retention_s`, `runtime.trace.drain_timeout_s`; positive integer `runtime.trace.queue_limit` and `workers`; conditional positive-sampling `runtime.monitoring.queue_limit`, `workers`, `drain_timeout_s`; existing `storage.data_root`/format and attachment-only KB hold settings. Examples retain unfitted sentinels, with explicit startup refusal when consumed.

**Compute.** CPU; durable trace writing is submitted without awaiting disk I/O on the response path. CPU tests use labelled doubles; live behavior and performance remain unmeasured.

**Depends on.** S5.

**Failure modes.** A status that hides a degradation (each status has a test, R-024); attachments that point at a collected snapshot (prevented by retention while traces reference it); traces that carry personal prompt text beyond retention (`deploy_required`).

**Experiments.** E13 (end-to-end latency and throughput).

**References.** None beyond the contracts.
