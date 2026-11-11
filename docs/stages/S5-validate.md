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
- **Repair.** At most `validation.max_repairs` rounds (a configuration profile value; the example configuration profile holds the user's baseline of one), never an unbounded loop: resend the draft with the failed entries, then rerun the whole ledger.
- **Fallbacks** in the order `validation.fallbacks` sets: a minimal output valid against the contract, or the user's original prompt with status `degraded`, or `failed`. A truncated or failed draft is never `success`.

**Decisions and configuration keys.** `validation.*` (gating conditions, sampling share, repair bound, fallback order), `vlm.roles.verifier`. The verifier is calibrated on a human-checked sample before its verdicts gate anything [A].

**Compute.** CPU for structural checks; one verifier call when gated in, plus one writer and one verifier call per repair. Not implemented; nothing measured.

**Depends on.** S2, S3, S4, P.

**Failure modes.** The verifier misses a negation or miscounts (structural checks run first; E7 measures it); the verifier favouring text written by the same model [A]; a repair that fixes one entry and breaks another (the whole ledger reruns).

**Experiments.** E7, E9, E12.

**References.** N100, N118, [N133](../note/N133-edit-evaluation-benchmarks.md).
