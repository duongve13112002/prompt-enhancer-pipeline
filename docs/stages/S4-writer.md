# S4 Writer: the admin's prompt plus the Enhancement Brief

**Purpose.** Write the enhanced prompt or edit instruction in the admin's style and output format (R-004), using the user's intent and only the knowledge S3 allowed.

**Input and output.** The active admin profile, the Intent IR, the Knowledge Pack, the source image for `edit`, then a draft in the profile's output contract.

**Method.**
- **File format [P]:** the manifest's `template_format` decides the split ([P](P-profiles.md)). `sectioned`: the `[SYSTEM]` section is the system message and the `[USER]` section is the user-turn template that holds the input variable; `[META]` is not sent as text. `plain`: the whole file is the system message and the brief is the user message.
- **System message:** the admin's system prompt (the `[SYSTEM]` section, or the whole file when `plain`) verbatim, plus an optional runtime addendum when a conflict was decided `config_wins` ([P](P-profiles.md)). The addendum is generated from configuration, versioned and visible in the trace; the result is called the effective prompt, never the admin's original prompt. The system message comes first and stays stable per profile, so backend prefix caches can reuse it.
- **User message:** the **Enhancement Brief**, rendered per the manifest into the admin profile's input variable (for the example profile, `{{original_prompt}}` inside the `[USER]` template). It holds the raw prompt verbatim; a reference-knowledge block of delimited data, one fact per line bound to its receiving entity; a constraint block (counts, negations, literal texts, creativity limits); for `edit`, the delta and the preserve list; and for `attach`, one note per attached image. Knowledge and constraints are delimited data, never instructions (R-026), because text inside images and knowledge can act as instructions ([N131](../note/N131-image-prompt-injection.md)). Quote semantics follow the manifest: with literal-text quotes, concept names appear without quotation marks.
- **Images:** the source image for `edit`; knowledge-base images only for `live_look` (internal reading by the writer, [S3](S3-knowledge.md)).
- **Edit practice:** name what stays instead of describing it, and point at a reference image for identity rather than describing a face ([N100](../note/N100-qwen-image-pe-i2i.md)). Fixed parts of the brief stay fixed and only open parts are filled ([N101](../note/N101-qwen-image-pe-t2i.md)); rewriters tend to over-infer, so creativity configuration limits what may be added ([N112](../note/N112-prompt-rewriters.md), R-006).
- **Output:** schema-constrained when the manifest has a schema and the backend supports it; otherwise free text checked in S5. An unsupported request field is dropped with warning `PARAM_UNSUPPORTED`.

**Decisions and configuration keys.** `writer.*` (thinking, sampling, token limit: `fit_from_bench`, E10, E11), `creativity.*`, `vlm.roles.writer`. On the path without knowledge, merging planner and writer is experiment E6, not the default.

**Compute.** One call to the `writer` backend; latency is set mostly by generated tokens and is measured on the deployment (R-019). Not implemented; nothing measured.

**Depends on.** S2, S3 (with retrieval on), P.

**Failure modes.** A truncated output (warning `TRUNCATED`, never `success`); backend error or deadline exceeded; the admin's prompt overriding a constraint in the user message (caught by the admin profile's lint, golden tests and S5); output that is not valid against the schema (`SCHEMA_INVALID`, sent to S5 repair).

**Experiments.** E6, E8, E10, E11, E12.

**References.** N100, N101, N112, N131.
