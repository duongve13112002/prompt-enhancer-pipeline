# S6 Envelope: status, output, attachments, trace

**Purpose.** Return one response with an honest status, the output exactly as the admin profile's contract defines it, and everything else outside the output (R-024).

**Input and output.** The checked output and status from S5, the Knowledge Pack's `attach` images, the source binding, the retrieval state, then the response envelope ([contracts](../../contracts/README.md)) and one `request_traces` row.

**Method.**
- **Envelope fields:** `status`; `output` (verbatim per the profile contract); `attachments` (per image `image_id`, role `knowledge_reference`, the entity it binds to, the facets to use; images are copies from the snapshot, never from the source tree); `source_binding` (the source image's `image_id`); `rag` with `state` and the pinned snapshot; `trace_id`.
- **Output stays clean:** no citations, warnings or debug fields inside `output`; warnings (`PARAM_UNSUPPORTED`, `SCHEMA_INVALID`, `TRUNCATED`, `SNAPSHOT_MISSING`, `PROFILE_CONFLICT`) go to the trace.
- **Status** comes from S5, or `needs_clarification` from S2 when an edit target is ambiguous (with the candidate targets) or when `resolution.on_family_match` is `ask` and a mention is a family match (with the candidate concepts).
- **Retrieval state [P]:** `disabled` when `rag.enabled` is false; `skipped_no_knowledge_needed` when the planner lists no knowledge need; `no_match` when no mention resolved to a concept, or concepts resolved but no fact passed the filters; `retrieved` when at least one fact or image was assembled (definitions: [contracts](../../contracts/README.md#enums)); `failed` when a knowledge access failed, the snapshot was missing included.
- **Trace:** effective configuration hash, profile and addendum versions, `snapshot_id`, shortlist, Intent IR, family and variant ratings, seeds and picks, Knowledge Pack with left-out reasons, brief, ledger verdicts, repairs, model revisions per role, timings.

**Decisions and configuration keys.** `runtime.*` (trace retention, `deploy_required`), `storage.data_root`.

**Compute.** CPU; the trace write must not block the response [P]. Not implemented; nothing measured.

**Depends on.** S5.

**Failure modes.** A status that hides a degradation (each status has a test, R-024); attachments that point at a collected snapshot (prevented by retention while traces reference it); traces that carry personal prompt text beyond retention (`deploy_required`).

**Experiments.** E13 (end-to-end latency and throughput).

**References.** None beyond the contracts.
