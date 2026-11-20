# S5 Validate: structural checks, gated verifier, bounded repair

**Purpose.** Prove that the draft keeps the user's intent and the profile's contract before it leaves (R-001, R-024, R-027), with exact checks first and a model verifier only where risk calls for one.

**Input and output.** The draft, the constraint ledger from S2, the Knowledge Pack, the manifest's validators, then a checked output, a per-entry verdict in the trace, and a status (`success`, `repaired`, `degraded`, `failed`).

**Method.** Each ledger entry has an ID, a type, the source span and its check. Structural checks are done directly; checks that need meaning are judged by the verifier. No word list or negation lexicon is used (R-015).

| Constraint type | How it is checked |
| --- | --- |
| `literal_text` | directly: exact string equality after decoding the output structure |
| `passthrough_token` | directly: present unchanged, or transformed as the manifest declares |
| `format` | directly: parse, schema, the manifest's validators |
| `negation`, `count`, `relation`, `attribute_binding` | by the verifier; directly only where the manifest declares a structural field for it |
| `style` | by the verifier |
| `identity_grounding` | by the verifier: every identity attribute in the output comes from the user or from a fact in the Knowledge Pack |
| `edit_coverage` | by the verifier: every operation of the delta is present |
| `edit_scope` | by the verifier: no operation outside the delta (leakage) |
| `edit_strength` | by the verifier: the change is specific enough not to be mistaken for the unedited image (under-editing) |

- **Verifier.** One text-only call that answers each entry yes or no with the output span it relies on, following dependency-structured yes/no checks ([N118](../note/N118-structured-checks.md)). The edit checks cover the two symmetric failures, leakage and under-editing ([N100](../note/N100-qwen-image-pe-i2i.md)).
- **Gating [P].** The verifier runs inline when identity facts from the knowledge base were supplied, the task is `edit`, the admin profile has a known conflict, or a ledger entry needs meaning; other requests are sampled after the response for monitoring (`validation.verifier.sampled_fraction_for_rest`, `fit_from_pilot`). Experiment E7 compares always, risk-gated and never.
- **Repair.** At most `validation.max_repairs` rounds (a configuration profile value; the example configuration profile holds the user's baseline of one), never an unbounded loop: resend the draft with the failed entries, then rerun the whole ledger. Repair round k also changes `sampling.seed` to `attempt_seed(seed, k)` (T017-FIX2, decisions 183 and 188), so the rounds are pairwise distinct draws; the repair data is the only other change. A writer role without an integer seed resends unseeded. A backend that drops the seed (`PARAM_UNSUPPORTED`/`sampling.seed`, e.g. `anthropic`, whose protocol has no seed field: CLOUD-1, decision 195 round) still repairs, because the repair data already makes each round a different request. Its warning is kept, and its attempt entry records the sent seed as null (T017-FIX2 fix round 1, PM decision pending user review). Every writer call record (`record_json`) carries `attempt: {attempt, seed, code, reply_digest}`.
- **Fallbacks** in the order `validation.fallbacks` sets: a minimal output valid against the contract, or the user's original prompt with status `degraded`, or `failed`. A truncated or failed draft is never `success`.

**Decisions and configuration keys.** `validation.*` covers gating conditions, calibration policy, sampling share, repair bound and fallback order. `vlm.roles.verifier` binds the verifier model.

**Compute.** CPU for structural checks; one verifier call when gated in, plus at most one writer and one verifier call per repair. The P2 library is implemented with CPU doubles; no live quality or performance measurement is claimed.

**P2 library.** `validation.validate` returns closed `validation_result.v1` (call entries per `writer_result.v1`, T017-FIX), with the unchanged profile payload, one stage status, verdict/attempt/call diagnostics, repair count, fallback attempts and a deferred-monitoring selection flag. It owns no client or background worker. A missing IR goes straight to permitted deterministic fallbacks, without fabricating a plan. Upstream degradation and budget fallback survive successful local repairs.

The profile contract runs before any verifier call. Literal/token/name values come from the IR, never a quote parser; declared JSON is decoded before equality checks. Declared exact-text selectors can prove a literal value, and passthrough tokens must be present unchanged. Named-concept placement, remaining rendering and unmapped format/meaning obligations require a verifier. Each structural artifact has a collision-free verdict ID; failed operator/parameter metadata reaches repairs as data. Code-owned edit, identity and conflict obligations close omissions in the planner ledger; their IDs cannot collide with planner IDs. Gating rules and `inline_for` are a union; omitting a required semantic trigger leaves the result unverified and uses fallbacks.

Eligibility requires pinned model routes for both roles, including every permitted backend fallback, and matching actual call provenance. Writer and verifier may share a model (decision 153). The required boolean `validation.verifier.require_calibration` controls inline verification (decision 155). True requires matching `verifier_calibration.v0` evidence from `validation.verifier.calibration_ref` and uses degraded fallback or fails if it is unavailable. False allows inline verification without matching evidence and emits `VERIFIER_UNCALIBRATED` in the trace and public envelope. The artifact binds human-checked evidence, routes, role settings, brief/verifier/admin/addendum prompts and ledger schema. Complete ID coverage and exact output evidence are required under both settings. Invalid or failed replies go to fallback. A caller can pass a verifier client or let S5 bind it from the writer's shared run after eligibility.

Every repair reruns the entire ledger under the unchanged admin system and addendum. Schema-invalid writer replies may use a configured repair with a null prior draft. Refusal, truncation and operational binding or call failures go to fallback. Minimal fallback requires an admin-confirmed, activation-hashed template containing an `original_prompt` insertion. It is substituted once, checked against the profile and always degraded. Monitoring is selected from the configured seed and request ID and still requires matching calibration when `require_calibration` is false. The delivery lifecycle owns monitoring execution.

**Depends on.** S2, S3, S4, P.

**Failure modes.** The verifier misses a negation or miscounts (structural checks run first; E7 measures it); the verifier favouring text written by the same model [A]; a repair that fixes one entry and breaks another (the whole ledger reruns).

**Experiments.** E7, E9, E12.

**References.** N100, N118, [N133](../note/N133-edit-evaluation-benchmarks.md).
