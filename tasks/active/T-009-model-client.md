# T-009 Model client

Kind: feature
R-IDs: R-018, R-015, R-021, R-024, R-026, R-027, R-004, R-012
Task file: tasks/active/T-009-model-client.md
Approach note: .tmp/T-009/research.md (the scout's research and hazard note C1-C3, copied into the worktree)

Starts once T-005 is accepted (decision 96; T-002 is accepted). Odd pipeline task: Codex implements; Gemini 3.8 Flash reviews first and Claude reviews second, and the patch is never integrated on the Gemini pass alone (decision 91, family split). While the user is away a product-meaning question gets the PM's conservative, reversible choice, logged as a PM decision pending user review, unless it changes a requirement (decision 94).

## Brief header

- **Goal:** the HTTP model client every model call goes through (`tasks/IMPLEMENTATION_PLAN.md:17`, `contracts/vlm-request.md`, `docs/rules/VLM_CALL_RULES.md`):
  - one role-bound call that builds the engine-neutral request from a validated role and its bound backend, and provider adapters for `openai_compatible`, `anthropic` and `gemini`;
  - a capability probe at start, with its result stored;
  - unsupported fields dropped with `PARAM_UNSUPPORTED` under `unsupported_param_policy: warn`, and `extra_body` combined last, with the effective body recorded and hashed;
  - structured-output post-validation against the versioned local schemas, an invalid reply stored as `SCHEMA_INVALID` and never repaired by guessing (R-024, R-027);
  - per-backend concurrency and cloud request budgets: every call, probe and retry counts, exhaustion falls back with `BUDGET_EXHAUSTED` or stops as configured, and `allow_images: false` keeps image requests local;
  - one shared client-side image resize (decision 84: `vision.max_image_side`, `none` keeps the original, an integer keeps the aspect ratio and never upscales, role override `image.max_side`), which the K1 embedder of T-008 will also use;
  - a call record per call (`contracts/vlm-request.md:61`) with no secret in it.
  - Every operational value (timeouts, retries, backoff, probe limits) is a validated configuration key with an override and a refusal test, never a library default (R-015). The admin system prompt passes byte for byte (R-004), image roles stay separate (R-012), and knowledge and image text stay data (R-026).
- **Done check:** the new tests pass through `tools/agent-workflow/test_slot.py` on the project venv python, covering:
  - the exact request per provider and role, for every sampling, reasoning, image and output field, the string `none` semantics, the `extra_body` collision with the effective-body hash, and the writer format resolved from the manifest;
  - a labelled in-memory or bounded loopback HTTP double for routes, capability replies, dropped-field warnings, malformed JSON, schema-invalid replies, HTTP errors, truncation, timeouts and cancellation; a probe that is incomplete or failed is distinct, and an HTTP 200 alone is never proof of a capability;
  - a check of every schema under draft 2020-12, with local `$ref` resolved without network;
  - property tests for the resize (portrait, landscape, square, tiny, `none`);
  - bounded concurrent calls that never overspend a budget;
  - the fallback paths, with the effective backend recorded;
  - a non-default override and a refusal per operational key;
  - 3 to 10 mutants of the changed checks.
  - Live E12 on the dev GPU is a separate later step under the `gpu-live-run` skill, `not_run` in this patch.
- **Scope:** the package from T-005 (`src/prompt_enhancer/`) with new model-client modules and their tests, the T-005 schema and read sets for new operational keys only (proposed in Step 0 with exact text), the CPU dependency declaration if Step 0 adds one, `.tmp/T-009/` of the worktree, and this task file. Read-only otherwise: `configs/`, `contracts/`, `docs/`, `tasks/REQUIREMENTS.md`.
- **Out of scope:**
  - stage logic: planner, writer, verifier, observer, namer, cards, aliases and profiles (T-010 to T-015);
  - starting or installing any model server; GPU work; any cloud call;
  - `.secrets/`: a key file path is validated, never opened by the worker;
  - `database/` and `tools/agent-workflow/`.
- **Output:** the patch in the worktree (unstaged), the Completion report in this task file, a reply under 15 lines.
- **Step budget:** about 80 tool calls after Step 0 approval.
- **Run limit:** CPU only; no paid calls; tests bounded to seconds; the HTTP double binds only to loopback.
- **May assume / Must ask:**
  - May choose module, class and function names and the test layout.
  - Must ask (Step 0 questions) about:
    - any library beyond Pillow and httpx, which may be installed into the project `.venv` only (decision 102);
    - each new configuration key and its sentinel;
    - the sync or async shape;
    - each wire mapping the primary documentation does not settle;
    - every contract drift listed in C2.
- **Role pack and CLI wrapper:** implementer; Codex (profile `codex-dev`).
- **Test sets:** not applicable: the pipeline suite under `tests/` is the one T-005 created; run it all plus the new tests.
- **Trivial:** no. **Small:** no.
- **Base:** commit 53535a3, which accepts T-005; working folder `.tmp/worktrees/T-009`.

### Research required before code

1. The scout's note `.tmp/T-009/research.md`: re-read every line it cites before relying on it (R-021), and work from the accepted T-005 code, not the unfinished code the scout read.
2. The primary API documentation of each provider mapping (OpenAI-compatible chat completions as served by llama.cpp and vLLM; Anthropic Messages; Gemini generateContent), with the URL and exact quoted words. Every cell of `contracts/vlm-request.md:39-53` is an assumption until then. If the worker has no network, Step 0 lists the exact pages needed and the PM fetches them.
3. The installed versions of the chosen libraries (`python -m pip list` inside `.venv`), quoted.

### Hazards the Step 0 plan must resolve (from the scout, C1-C3)

- **C1:** the provider mappings and engine behaviour for image, schema and thinking combined are unverified (`contracts/vlm-request.md:22,39`; notes N055, N057, N121, N122). The CPU double proves only mapping consistency; E12 proves the dev engine.
- **C2:** the contracts drift in three places:
  - the resize prose still says `fit_from_bench` (`contracts/vlm-request.md:57`, `K2-observe.md:14`), against decision 84;
  - `docs/test-plan.md:10` says unsupported fields are refused, against the binding warn-and-drop contract (`vlm-request.md:10`);
  - endpoint joining, timeouts, retries, probe bounds, decode and rounding policy, and budget persistence are undefined.
  - Proposed: explicit validated keys with sentinels; the contract edits go to the PM.
- **C3:** budgets and concurrency must be atomic across concurrent calls, fallbacks must re-probe the local backend, retried offline work must never look committed before a valid artifact exists (`docs/execution.md:66`), and close and cancellation need bounded tests on both OSes.

### Configuration impact

Fill before the first edit: every key the client reads, its schema, its example value, its override and refusal tests, and its classification.

## Step 0

### Unknowns and failure modes

- **C1 (R-018/R-021):** every wire mapping and image/schema/thinking combination remains unverified. The bounded HTTP double proves the adapter contract only; HTTP 200 does not prove a capability. E12 remains a separate live step.
- **C2 (R-015):** endpoint joining, retries, probe bounds, decode, rounding and budget persistence are unspecified. `docs/test-plan.md:10` says refusal while `contracts/vlm-request.md:10` requires warn/drop; the scout's resize drift is stale: current `contracts/vlm-request.md:57` already says `none`, while `docs/stages/K2-observe.md:14` still needs PM verification/correction. Contract edits are PM-owned. Schema files live under `contracts/`, which is both named for the T-005 schema edit and otherwise read-only in this brief; approval must resolve this scope conflict.
- **C3 (R-024/R-027):** racing calls/retries/probes can overspend, fallback may use stale capabilities, cancellation can leak resources, and a failed response must not appear as a committed artifact. Effective bodies may contain private data: records must exclude credentials and be handled by the caller's authorized storage.
- Accepted `src/prompt_enhancer/config.py` uses draft-2020-12 validation, explicit `COMMAND_READS`, sentinel refusal and `resolve_writer_format`; `.agents/project.json` still says runtime config module null. No cloud credentials, GPU, model server or network were used here.

### Ranked questions for PM, with proposed answers

1. **Wire semantics and authorization:** Please supply primary documentation and decide every unresolved provider/engine mapping, including capability evidence and authentication; proposed: implement only documented mappings, warn/drop unsupported fields, and fail closed for an unproven required capability. Exact pages to fetch: https://platform.openai.com/docs/api-reference/chat/create ; https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md ; https://docs.vllm.ai/en/latest/serving/openai_compatible_server/ ; https://docs.anthropic.com/en/api/messages ; https://ai.google.dev/api/generate-content . Pin the applicable engine/API versions and record exact quotations before implementation. Do not infer support from a successful status or a double.
2. **New keys and scope:** Approve the exact proposed keys, sentinels and schema/read-set edits in the table below, including the necessary exception to `contracts/` read-only for `contracts/schemas/enhancer_config.v0.schema.json`; examples under `configs/` are read-only, so PM must add the matching fields to all affected profiles or explicitly expand scope. Proposed: required keys and unresolved `design_required`/`deploy_required` sentinels, refused before use; no hidden defaults. Explicitly decide whether overlays may override the new backend keys.
3. **Budget and lifecycle:** May the API be asynchronous and budget ownership be a shared, explicitly supplied run-scoped atomic ledger, without persistence across independent runs? Proposed: yes, `httpx.AsyncClient`, one ledger shared by all clients in a run; caller persists/reconciles grants before resume and no cloud call is allowed without that ledger. Each attempt, including probes and failures, reserves before sending; fallback probes the effective local backend. Cross-process/resume safety is not established by an in-process lock; require a caller-provided durable atomic ledger for shared grants or refuse that use.
4. **Unspecified policies and drift:** Approve URL join as an explicit base URL plus configured relative endpoint, rejecting ambiguous or non-HTTPS non-loopback URLs; retry only explicitly configured retryable status codes/transport failures with bounded exponential backoff and no implicit library retry; failed/incomplete probes stay distinct. Proposed resize: Pillow decode errors explicit, EXIF orientation applied, LANCZOS resampling, integer dimensions rounded down with minimum 1, no upscale; `none` retains original bytes when no re-encoding is requested. PM owns K2 resize prose and test-plan warn/drop correction, and decides encoding/decode limits and trace retention. No inference from stale `fit_from_bench` prose.
5. **Dependencies:** Approve `httpx==0.28.1` and `Pillow==11.3.0` in project `.venv` after approval, with `jsonschema==4.26.0`, `referencing==0.37.0`, and `hypothesis==6.170.15` already present; no provider SDK. Proposed: use existing jsonschema/referencing for offline schema validation and Hypothesis for tests, declare CPU dependencies with exact versions and verify installed source/behavior after installation. `pip list` also reports PyYAML 6.0.3 and pytest 9.2.0; Pillow/httpx absent. Versions proposed, not tested or claimed installed.

### Configuration impact (proposed; approval required before runtime edits)

Existing configuration schema is `contracts/schemas/enhancer_config.v0.schema.json` and role schema `contracts/schemas/vlm_role.v0.schema.json`, loaded by `src/prompt_enhancer/config.py`; example source is `configs/enhancer.example.yaml` plus dev/overlay profiles as applicable. Each row is **configurable**; implement a non-default effect test and missing/invalid/sentinel refusal test per key, including cross-field checks. Existing values below are examples, not new defaults. Tests use synthetic profiles because profiles are currently outside scope.

| Key(s) | Schema / example and proposed tests |
|---|---|
| `vlm.default_backend`, `vlm.roles.<role>.{mode,backend,prompt_id,max_tokens}` | Existing role/config schemas; `main`, `generate`; override role/backend/tokens and refuse missing, unknown backend or invalid mode. |
| `vlm.roles.<role>.sampling.{temperature,top_p,top_k,min_p,presence_penalty,frequency_penalty,repetition_penalty,seed,stop}` | Existing role schema; per-role examples in profile; test each mapping and non-default, missing/invalid, and literal `none` where permitted. |
| `vlm.roles.<role>.reasoning.{mode,effort,budget_tokens,template_kwargs_key}` | Existing role schema; examples in profile; test each override/refusal and `none` semantics. |
| `vlm.roles.<role>.image.{source,max_side,format,quality,transport}`, `vision.{max_image_side,format,quality,transport}` | Existing role/config schemas; `max_image_side: none`, `max_side: from_vision`; override each and refuse invalid/missing; property-test no upscale, aspect ratio and role override. Image `none` is valid for text-only roles. |
| `vlm.roles.<role>.output.{format,schema_id,strict,transport,validate_response}` | Existing role schema; writer `from_profile_manifest`; test all overrides/refusals, manifest resolution and local schema validation; schema IDs resolve within project. |
| `vlm.backends.<name>.{provider,base_urls,api_key_file,model.id,model.revision,model.source,model.file,model.mmproj_file,max_concurrency,unsupported_param_policy,extra_body,endpoints}` | Existing config schema; `provider: openai_compatible`, `unsupported_param_policy: warn`, `max_concurrency: fit_from_bench`; test per-field override/refusal where client reads it, endpoint selection, merge-last hash, and secret exclusion. Model artifact paths are server-owned, not opened by client. Confirm exact currently optional fields and endpoint shape before implementation. |
| `vlm.backends.<name>.{call_budget,on_budget_exhausted,allow_images}` | Existing config schema; `ask_user`, `fallback_to_default_backend`, `true`; test override/refusal, atomic concurrent reservation and local fallback. |
| `vlm.backends.<name>.http.connect_timeout_s`, `.read_timeout_s`, `.write_timeout_s`, `.pool_timeout_s` | **New**, required finite number > 0 or `deploy_required`; example `deploy_required`; each overridden timeout observed by double, missing/zero/nonfinite/sentinel refused. |
| `vlm.backends.<name>.http.max_retries`, `.backoff_initial_s`, `.backoff_max_s`, `.retry_statuses` | **New**, required integer >= 0, finite numbers >= 0 with max >= initial, and array of valid HTTP status integers respectively, each or `design_required`; example `design_required`; each override changes attempt/backoff behavior, missing/invalid/sentinel refused. Automatic transport retries disabled. |
| `vlm.backends.<name>.probe.max_requests`, `.timeout_s` | **New**, required positive integer / finite positive number or `design_required`; example `design_required`; override bound and refusal per key, incomplete versus failed distinguished. Probe attempts also consume budget. |
| `vlm.backends.<name>.image.max_decode_pixels` | **New**, required positive integer or `deploy_required`; example `deploy_required`; override bounds decoding, reject missing/invalid/sentinel and oversize image. |
| `vlm.backends.<name>.anthropic_version` | **New** per PM's authentication instruction: required for Anthropic, nonempty header-safe string or `deploy_required`; example `deploy_required`, deployment-overridable. Test non-default header transmission, missing/invalid/sentinel refusal and credential exclusion from records. Configurable. |

No new image rounding, persistence, endpoint-join or HTTP-status policy is silently selected: question 4 proposes them for explicit PM resolution. `storage.data_root` and run artifact persistence remain caller-owned; no client-owned path, device, GPU, workers or model loading is introduced. Protocol constants and safety invariants will be classified in the final audit; test fixtures are test-only. If new implementation reveals another operational setting, stop its use until an approved schema/read-set and tests exist.

### Plan and finish checks

1. After approval, obtain and quote primary sources, settle all questions and scope, inspect accepted loader/schema/callers and installed library behavior. Add approved required keys/read sets, dependency declaration and synthetic profile fixtures; first run focused config override/refusal tests through `test_slot.py`. No product implementation rests on an unresolved mapping.
2. Implement shared resize, offline local schema registry and async role-bound adapters; resolve writer format using accepted loader, preserve system prompt bytes and data roles, apply merge-last and hash the effective body. Test all fields/providers/roles, `none`, collisions, every schema and resize properties.
3. Implement bounded capability probes, cloud budget ledger and per-backend concurrency, fallback and lifecycle; test labelled in-memory/loopback doubles for capabilities, HTTP/decode/schema/truncation/timeouts/cancellation, atomic budgets and effective backend. No process window; loopback only; failed offline output never implies commit.
4. Run entire pipeline suite and new tests via project venv and `test_slot.py`, lint/format checks where available, 3–10 scratch mutants, inspect secrets/literals and fill the row-by-row configuration audit and completion report with actual output. Report Windows/Linux coverage separately. E12/GPU/cloud remain `not_run`; stop on repeated failure per footer.

Evidence: `pip list` returned jsonschema 4.26.0, referencing 0.37.0, hypothesis 6.170.15 and no Pillow/httpx; `recall.py build` returned `indexed=84 unchanged=0 removed=0 refused=0 chunks=418 skipped_chunks=0`, and search `model client timeout probe budget resize provider` returned no output. Primary provider pages and live behavior are **not verified**; no tests, installs or model calls were run in Step 0.

## Continuation Step 0 (awaiting PM approval)

### PM decisions pending user review (2026-11-15)

- Validate role/backend provider constraints, default local backend, and fail-closed provider capabilities during `load_config`; constrain Anthropic thinking only for roles bound to Anthropic. No unverified provider base URL is inferred: cloud `provider_default` is refused with a precise message.
- Only `image.max_side` may be omitted and resolved from vision; other image-object members must be explicit. Validate available Pillow encoders at load; enforce vision quality 1–100 and nonfinite sampling refusal. Empty model ID/revision are refused by schema.
- Treat model artifact source/files, health endpoint, and image source/transport and output transport as owned by their stage tasks; record each owner with evidence from the implementation plan. These decisions are reversible and pending user review.

Consumer ownership proposed from `tasks/IMPLEMENTATION_PLAN.md`: `endpoints.health`, `model.source`, `model.file`, `model.mmproj_file` — consumer: T-016 (deployment entry points and run guide); image `source` — consumers: T-010 (K2 observation), T-014 (planner/assembly); image `transport` — consumers: T-010/T-014 (image call construction); output `transport` — consumer: T-015 (writer and validation). The plan names these stage/deployment tasks but does not assign every individual key explicitly; these key-level assignments are PM decisions pending user review, not independently established plan facts.

| Key not read by T-009 client | Consumer: another task |
|---|---|
| `endpoints.health` | T-016 |
| `model.source` | T-016 |
| `model.file` | T-016 |
| `model.mmproj_file` | T-016 |
| role `image.source` | T-010 / T-014 |
| role `image.transport` | T-010 / T-014 |
| role `output.transport` | T-015 |

### Configuration impact: provider and image validation continuation

Schemas: `enhancer_config.v0` and `vlm_role.v0`, profiles `configs/enhancer.example.yaml`, inherited dev and deployment overlays. Configurable paths affected: `vlm.default_backend`; backend `provider`, `base_urls`, `model.id`, `model.revision`; role `backend`, `max_tokens`, `sampling.*`, `reasoning.mode`, `reasoning.budget_tokens`, `image.{source,max_side,format,quality,transport}`, `output.{format,schema_id,transport}`; `vision.{format,quality,max_image_side}`. Provider capability checks must use the bound backend and existing sentinels; examples retain unresolved values as sentinels. Non-default and refusal cases will use a table through real `load_config`, covering local and cloud role binding, provider-specific budget, fail-closed image/output, unspecified endpoint, image encoder and required image members, finite sampling, size inheritance and quality bounds. The existing 1024 Anthropic bound is protocol-bound as recorded in provider notes; encoder support is a locally detected structural capability, not a configurable default. No network, credential, GPU or additional dependency is authorized.

### Configuration impact: PM schema-gap correction

The PM authorized correcting existing model-client schema bounds where the consumer requires a narrower value. `vlm.backends.<name>.max_concurrency` is configurable, declared in the enhancer config schema and example profiles, and `BackendConcurrency` requires an integer >= 1. Change its schema minimum from 0 to 1; `test_config_values_through_profile_load` supplies override 3 and refusal 0. Other gaps found by the requested sweep will be recorded with their affected schema, profile and tests before editing them. No runtime consumer default is added.

`vlm.backends.<name>.base_urls` is configurable and the transport reads index 0: require at least one element in the config schema, with a nonempty override and an empty-list refusal in `test_config_values_through_profile_load`. `vlm.backends.<name>.endpoints.chat` is configurable and the client indexes it: require the `endpoints` object in the config schema, use the existing `deploy_required` sentinel in each base example backend, and test a concrete endpoints override plus missing/sentinel refusal through `load_config`. Dev and deployment profiles inherit the example or supply deployment overrides. The schema already requires both `chat` and `health` inside an object.

The same transport rejects empty base and chat strings, and the cloud credential loader requires exactly `.secrets/<filename>`: constrain these values in the config schema, retaining its existing deployment sentinels. Example cloud profiles already use that path form; add non-default and refusal cases to the profile-load table. This is lexical validation only; tests do not access credential directories.

### Unknowns and failure modes

- R-015: The existing audit identifies configuration rows with missing profile-load override/refusal coverage. Some settings may validly be absent, conditional, unconstrained maps, or unused by this client; inspect their schemas and actual consumers before choosing a meaningful invalid value or asserting runtime effect. A schema-only assertion does not establish an effect.
- R-018/R-021: The provider notes identify unresolved cloud mappings and an endpoint-specific authentication caveat. Their quotations are a research lead, not proof that a new mapping is supported. Retain precise fail-closed behavior when primary evidence cannot be checked.
- R-024: Previously reported tests and mutations are historical results, not evidence for this continuation. The current patch includes untracked runtime modules; a tracked-only diff would miss their literals and changed lines.
- Environment: `.tmp/T-009/approve-step0.md` was not found, although this dispatch states the earlier plan was approved. `.venv/Scripts/python.exe` is absent in this worktree; the required recall build could not start. The dispatch itself authorizes the plan-only continuation, but the location of the project venv is unknown.

### Ranked questions and proposed answers

1. **Project interpreter:** Where is the approved project venv interpreter available to this worktree? Proposed: use the already provisioned project venv if the PM identifies its path and confirms it is permitted; do not install or substitute a system interpreter. Without it, recall, tests, lint and mutation runs remain `not_run`.
2. **Prior approval evidence:** Can the PM provide the missing `.tmp/T-009/approve-step0.md` or its correct location? Proposed: preserve the dispatch's explicit statement of approval, and check the actual approval before any work that depends on its details; do not infer additional scope.
3. **Unsettled provider mappings:** If cited primary pages do not resolve a mapping, may it remain precisely refused? Proposed: yes, as explicitly permitted by this dispatch; record the feature and refusal, and do not claim full cloud support or live verification.

### Plan and checkable finish

1. After approval and interpreter resolution, run the required recall build/search; inspect the earlier approval, configuration loader, schemas, example profiles, existing parameterized tests, runtime consumers and the cited provider notes. Inventory every audited key, its conditional validity and expected observable effect. No new keys or dependencies are assumed.
2. Add a table-driven test through the real profile load path for each applicable operational and role key: set a valid non-default value and observe it at the relevant boundary, then exercise a genuinely invalid or required-missing value and verify refusal. Record conditional or inapplicable cases honestly; fix defects within scope and stop for a requirement change or an unresolved mapping.
3. Inventory literals across all new and modified runtime files, including untracked files, and classify each in the row-by-row configuration audit. Re-read only already-cited primary provider pages if needed and permitted; quote exact words and URL for any mapping used, otherwise document the precise fail-closed behavior. Update the completion report without treating doubles as engine proof.
4. Run focused tests and 3–10 mutations of lines changed since the last mutation pass in scratch copies, via `test_slot.py`; report each survivor. Run available ruff lint and format checks, then the full pipeline suite via the limiter with `PYTHONPATH=src`. Record exact commands and output, skips, defects and unavailable tools; stop after the same step fails twice. Clean only scratch created by this continuation.

Finish requires the audit and named tests to account for every applicable key and literal, mutations and full suite results to be recorded, and unresolved defects to remain explicit. Linux, E12, real engine thinking/image support and live cloud calls are `not_run`; no GPU or model server is started. Status: **AWAITING PLAN APPROVAL**.
## Steps

### Review fix round 2: configuration impact (R-015, R-018, R-021, R-024)

- PM decision pending user review: add required `vision.alpha` to `enhancer_config.v0.schema.json` and the base example; dev and deployment overlays inherit it. A `refuse` policy rejects transparency when an encoder cannot preserve it; `flatten` requires a `#RRGGBB` background and composites alpha before encoding. Base example: `{policy: flatten, background: '#FFFFFF'}`. Test non-default background and `refuse`, missing or malformed settings, exact source-mode/target/policy outcomes and pixel values; role-bound model calls and shared vision image preparation must both read the same setting.
- `vision.format`, role image format/quality/max-side and backend decode bound remain existing validated values; no new silent defaults. Test resizing and configured effects with both alpha policies.
- Error codes at `RoleClient.call` are an artifact-bound public contract; preserve the original exception as `__cause__`, and preserve task cancellation. Credential link and hard-link checks for `api_key_file` are safety-bound. The reviewer-owned `.tmp/T-009-review/probe_review.py` lacks `anthropic_workspace_id` in its synthetic fixture and is not changed.
- Planned checks: focused tables and link tests, 3–10 scratch mutants per changed runtime module, full pipeline suite through `test_slot.py`, row-by-row audit and handoff. No network, GPU, real credentials or model calls.

### Review fix round 2: configuration audit

| Behavior | Validated configuration / example | Override and refusal evidence | Literal class |
|---|---|---|---|
| Transparency | Required `vision.alpha_policy` object: `{mode: refuse}` or `{mode: flatten, background: '#RRGGBB'}`; base profile uses white flatten, dev and deployment overlays inherit it. The shared image function and role client consume it. | `test_alpha_policy_profile_override_and_refusal`, `test_alpha_background_override_and_refusal`, `test_image_mode_and_format` | Modes, background, and setting key configurable; `#RRGGBB` validation safety-bound; fixture colors test-only. |
| Image format, dimensions and decode limit | Existing `vision.format`, role `image.{format,quality,max_side}` and backend `image.max_decode_pixels`; the policy also enters the request hash. | `test_image_mode_and_format`, `test_resize_dimensions`, `test_image_failure_is_coded_with_original_cause` | Configurable; encoder diagnostics safety-bound. |
| Call error contract | No new setting; stable `IMAGE_INVALID`, `CAPABILITY_UNAVAILABLE`, `BUDGET_EXHAUSTED`, `CONFIGURATION_INVALID`, `INPUT_INVALID`, `CLIENT_STATE_INVALID`, `MODEL_CALL_INVALID` and `MODEL_CALL_FAILED` prefixes retain the original exception as cause; cancellation remains cancellation. | `test_image_failure_is_coded_with_original_cause`, `test_call_wraps_escaping_exception_with_cause`, `test_call_cancellation_propagates` | Error codes artifact-bound; diagnostics safety-bound. |
| Credential link handling | Existing `api_key_file`; `os.lstat` rejects symbolic links, Windows reparse points and `st_nlink > 1` before reading. | `test_configured_link_refused_before_read`, `test_hard_link_refused_before_read`, `test_junction_refused_before_read`; actual symlinks skipped without rights. | Link checks and hard-link threshold `1` safety-bound; synthetic paths test-only. |

Reviewer's original `probe_review.py` remains unchanged and its fixture lacks `anthropic_workspace_id`.

The round-two runtime literal scan classifies alpha-channel names `A`/`a`, palette `transparency`, Pillow `RGBA`/`RGB`, RGB hex channel offsets and the fully opaque alpha value `255` as protocol-bound image representation; accepted mode names `refuse` and `flatten` and the configured background are configurable. Error prefixes are artifact-bound record/API vocabulary; error diagnostics, link type/attribute checks and the maximum hard-link count of one are safety-bound. Numeric dimensions and colors in tests are test-only. No unconfigured background or new runtime default is used.

Call-path audit: role lookup and writer resolution, backend/engine selection, credential loading and capability probes, required-capability check, image preparation, mapping/hash construction, fallback and budget exhaustion are inside the public exception boundary. Image failures and unproven capabilities receive their specific codes and retain the cause; budget, configuration, invalid input and incomplete client state receive separate codes, existing explicit client refusals receive `MODEL_CALL_INVALID`, and unexpected exceptions receive `MODEL_CALL_FAILED`. Transport exceptions already returned as `HTTP_ERROR` records and response parse/validation failures returned as `SCHEMA_INVALID` records do not escape; asynchronous cancellation is intentionally not converted.

**Round 2 checks so far (unit, Windows):** through the worktree `test_slot.py`, `tests/test_model_client.py tests/test_model_image.py tests/test_model_secrets.py tests/test_model_config.py::test_alpha_policy_profile_override_and_refusal` returned `195 passed, 3 skipped, 4 warnings in 3.54s`; the 3 skips are real symlinks without link rights, and warnings are Pillow's `I`-mode PNG deprecation. The expanded image failure/code checks returned `18 passed in 1.56s`; exception/cancellation checks returned `6 passed in 2.42s`. The two generic field-contract checks initially failed because the new conditional policy requires removing `background` to switch from `flatten` to `refuse`; after explicitly representing that condition and a valid alternate color, their retest returned `2 passed in 51.88s`.

**Round 2 mutants:** nine scratch-copy mutants, three each in `model_image.py`, `model_client.py` and `model_secrets.py`, were killed through `test_slot.py`: alpha detection, refusal, background composition; image code, image cause, budget code; hard-link count, symbolic link, Windows junction. No survivors. The iteration script was removed; scratch results under `.tmp/T-009/fix2-mutants/` remain for PM cleanup because the sandbox previously rejected removal. The Pillow mode exploration found `LAB` has an `A` color channel rather than alpha; alpha detection checks the last channel or palette transparency instead.

**Full pipeline (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/fix2-suite --tb=short` initially returned `2 failed, 1625 passed, 31 skipped, 4 warnings in 1230.63s (0:20:30)` for the two field-contract assertions described above. After correcting those assertions, the same command with `--basetemp=.tmp/T-009/fix2-suite-retest` returned `1635 passed, 30 skipped, 4 warnings in 835.26s (0:13:55)`. The final test-only additions after suite collection were run separately: `tests/test_model_image.py tests/test_model_client.py::test_image_failure_is_coded_with_original_cause tests/test_model_config.py::test_alpha_policy_profile_override_and_refusal` returned `231 passed, 4 warnings in 2.62s` through the limiter. The final exact matrix has 16 source cases, three targets, two alpha policies and two resize choices (192 cells), including `PA`, `LAB`, `I;16B`, `I;16L` and `YCbCr`. The latter modes were identified by inspecting the installed Pillow TIFF and IM loader tables; fixture format variants that Pillow normalizes to an already-covered loaded mode are not counted twice.

**Other checks:** `git diff --check` on the changed files exited 0 with no output. `<venv>/python -B -m ruff check ...` and `<venv>/python -B -m ruff format --check ...` each exited 1 with `No module named ruff`; lint and format are `not_run`. Real Python 3.11 execution, Linux, live GPU and cloud are `not_run`; the implementation uses `os.lstat`, `stat.S_ISLNK` and the Windows reparse-point attribute rather than `Path.is_junction`. The Windows junction and real synthetic hard-link tests passed; the three real symlink tests skipped without link rights. The first failure and its fix are preserved here, not treated as a green run.

**Current round-two status: REVIEWING.** Runtime changes are in `model_client.py`, `model_image.py` and `model_secrets.py`; the schema, base example, focused tests and this task file were updated. PM decisions on the error contract, alpha policy and credential checks remain pending user review. The PM must run skipped symlink cases with link rights and clean scratch under `.tmp/T-009/`; this worker's iteration scripts were removed, while the sandbox previously rejected directory cleanup. No network, model call, install or Git write was made. No new lessons.

### Review fix round 1: configuration impact

- **Workspace follow-up (R-015/R-018/R-021; PM decision pending user review):** `vlm.backends.<name>.anthropic_workspace_id` is required for Anthropic in `enhancer_config.v0.schema.json`; it accepts the explicit string `none` or a nonempty header-safe workspace ID. The base example uses `deploy_required` as an unresolved deployment sentinel, refused when the backend is selected; dev and deployment overlays inherit it and may override it. Test profile loading with a non-default ID, `none`, missing and unsafe values, sentinel refusal, and exact synthetic outgoing headers for both ID and `none`. No default is inferred. Schema and allowed overlay keys must both be updated.
- `vision.format` / role `image.format`: existing validated format selects the encoder; unsupported image modes will be refused with a precise error, without an implicit background or new setting. `max_side`, `quality`, and `max_decode_pixels` retain their existing validated behavior. Safety-bound encoder compatibility.
- `vlm.backends.<name>.base_urls`: existing validated list; refuse more than one entry until fail-over is designed. Configurable, existing profile values unchanged.
- `vlm.backends.<name>.api_key_file`: existing validated path; refuse links and unreadable files without displaying credentials. Safety-bound.
- Attempt warnings and prior attempts are record metadata, not configuration. Provider headers require primary-source verification before modification.

### Review fix round 1: provider documentation supplied by PM (2026-11-15)

- Anthropic [API overview](https://platform.claude.com/docs/en/api/overview): “Requests to the Claude API include these headers:”; `Authorization`: “`Bearer <token>`, where `<token>` is your API key”, required “Yes, unless `x-api-key` is set”; `x-api-key`: “Legacy fallback for `Authorization`, still supported”; `anthropic-version`: “API version (for example, `2026-10-21`)”, required “Yes”; `content-type`: `application/json`, required. The [Messages page](https://platform.claude.com/docs/en/api/messages) calls `anthropic-version` “optional string”; the client retains the header as required by the overview.
- Anthropic [API overview](https://platform.claude.com/docs/en/api/overview), PM re-read 2026-11-15: `anthropic-workspace-id` is the “ID of the workspace the request runs in”; it is “Required with an API key that isn't scoped to a workspace. Optional for other API keys.”
- Gemini [API key documentation](https://aistudio.google.com/docs/api-key) REST example: `-H "x-goog-api-key: YOUR_API_KEY"`; [generateContent reference](https://aistudio.google.com/api-reference/generate-content): `POST /v1beta/models/{model}:generateContent`. Neither supplied page states a version header is required for generateContent. These quotations supersede the earlier scout-only authentication evidence; no live call was made.
- The `Api-Revision` example on the Gemini key page belongs to the Interactions endpoint, not `generateContent` (PM re-read 2026-11-15).

### Review fix round 1: work and configuration audit (incomplete)

- **R-018/R-021, blocked:** `Invoke-WebRequest` to `https://docs.anthropic.com/en/api/messages` and `https://ai.google.dev/api/generate-content` each returned `Unable to connect to the remote server`. No provider header was changed; exact primary-source quotations are still required.
- **Image format and quality:** Existing `vision.format` / role `image.format`, `quality`, `max_side` and backend `image.max_decode_pixels` remain configurable. Unsupported mode/encoder combinations raising `OSError` now raise a mode-and-format `ValueError` instead of choosing an unconfigured alpha background (safety-bound). `test_image_mode_and_format` covers six PNG modes by PNG/JPEG/WEBP; `test_alpha_jpeg_refusal_at_any_size` is a size property. This is not yet a complete audit of all Pillow modes and encoders.
- **`vlm.backends.<name>.base_urls`:** multiple entries now refused at load until fail-over is designed; `test_config_values_through_profile_load` covers the single URL override and multi-URL refusal. The existing schema and example profiles remain in force; configurable.
- **`vlm.backends.<name>.api_key_file`:** a missing/unreadable file and a link are refused with the setting named; `test_missing_and_linked_credential_refused` uses synthetic files, but its link portion was skipped because link creation was unavailable. The existing synthetic override test passed; safety-bound.
- **Attempt metadata:** `previous_attempts` stores abandoned backend warnings and exhaustion; the final `warnings` retains the exhaustion marker and only the effective backend's parameter warnings. `test_fallback_warnings_belong_to_each_attempt` passed. Per-call local re-probe remains unchanged. Record keys/statuses are artifact-bound.
- **Focused checks:** `test_slot.py -- <venv>/python -B -X faulthandler -m pytest -q -p no:cacheprovider tests/test_model_image.py tests/test_model_secrets.py tests/test_model_client.py tests/test_model_config.py --basetemp=.tmp/T-009/fix1-tests --tb=short`: `156 passed, 1 skipped in 18.85s`. The subsequently added fallback and size-property tests each passed separately (`1 passed` each).
- **Review probe:** `test_slot.py -- <venv>/python -B -X faulthandler .tmp/T-009-review/probe_review.py` exited 0: P1 `ValueError | api_key_file is missing or unreadable`; P2 `ValueError | image mode RGBA cannot be encoded as jpeg`; P3 only `BUDGET_EXHAUSTED` remains in final warnings; P4/P4b report no key leak. The probe prints P2 as an error even though the brief permits a precise refusal.
- **Full pipeline (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/fix1-suite --tb=short` returned `1461 passed, 27 skipped in 400.92s (0:06:40)`. The two later-added tests ran separately as noted above. `git diff --check` on the changed files exited 0 with no output.
- **Not run / open:** primary-source authentication verification and header test; full class of image modes and encoders; link test on a system permitting symlinks; mutation tests; Linux; GPU and live cloud calls. `python -m ruff --version`: `No module named ruff`; lint and format checks not run. This round is not complete.
- **Status:** BLOCKED on provider documentation access; remaining independent image-class coverage, mutations, and audit must also be completed. Next: obtain exact provider quotations, fix and test headers, extend image-class coverage, run mutants and complete audit. Do not treat the review probe's exit code as proof that its P2 output was successful. No new lessons.

### Review fix round 1: continuation and configuration audit

The preceding blocked status and auth-evidence limitations are superseded by the PM-supplied quotations above. P2-1 keeps the existing authentication scheme. The Anthropic version is `vlm.backends.<name>.anthropic_version`, required by `enhancer_config.v0.schema.json`, with `deploy_required` in the example profile; the loader rejects omission or an invalid header value, and role binding rejects the unresolved sentinel. No version value is embedded in runtime source. `test_anthropic_version_override_and_refusal`, `test_cloud_headers_use_synthetic_credentials`, and `test_exact_cloud_request_headers_redacted` cover override, refusal and wire effect.

| Changed operational value or behavior | Configuration / validation / example | Override, refusal and effect evidence | Literal classification |
|---|---|---|---|
| Cloud credential and API version | `vlm.backends.<name>.api_key_file` and `anthropic_version`; schema required for cloud/Anthropic; example uses `.secrets/...` and `deploy_required` | Synthetic credential, `test_anthropic_version_override_and_refusal`, `test_cloud_headers_use_synthetic_credentials`, `test_exact_cloud_request_headers_redacted` | setting keys configurable; `Authorization`, `Bearer `, `anthropic-version`, `x-goog-api-key`, `content-type`, `application/json` protocol-bound; synthetic values test-only |
| Anthropic workspace selection | `vlm.backends.<name>.anthropic_workspace_id` is required for Anthropic by `enhancer_config.v0.schema.json`: nonempty printable ASCII without spaces, `none`, or an unresolved `deploy_required` example sentinel; selected-backend sentinel traversal refuses unresolved values. Dev and vLLM overlays inherit the example and may explicitly override this field. | `test_anthropic_workspace_override_and_refusal` proves loaded ID and `none` reach the header behavior and missing, empty, newline and non-ASCII values are refused; `test_overlay_engine_overrides` tests deployment override/refusal; `test_exact_cloud_request_headers_redacted` verifies outgoing headers; `test_cloud_headers_use_synthetic_credentials` and `test_selected_anthropic_workspace_sentinel_refused` reject the sentinel. | Field, ID and explicit `none` choice configurable; `deploy_required` configurable sentinel; `anthropic-workspace-id` protocol-bound; ASCII header guard safety-bound; test values test-only. |
| Credential link and unreadable path refusal | Existing validated `api_key_file`, one file beneath `.secrets`; directory and candidate links refused | `test_synthetic_credential_and_path_refusal`, `test_missing_and_linked_credential_refused`, `test_configured_link_refused_before_read`; real symlink test skipped without link rights | `api_key_file` diagnostic and link/path checks safety-bound; file and names in fixtures test-only |
| Backend URL selection | `vlm.backends.<name>.base_urls`, schema requires a nonempty list, example has one URL or a sentinel; loader refuses more than one until fail-over is designed | `test_config_values_through_profile_load`, `test_multiple_backend_urls_refused_with_setting_name` | `1` safety-bound cardinality guard; setting name configurable; diagnostic safety-bound |
| Image target, quality, resize and decode limit | `vision.{format,quality,max_image_side}`, role `image.{format,quality,max_side}`, backend `image.max_decode_pixels`; existing schemas and example profile; JPEG is the only concrete configured target in repository profiles | `test_image_mode_and_format` covers all eleven requested source modes against JPEG, PNG, WEBP; `test_alpha_jpeg_refusal_at_any_size`, `test_resize_dimensions`, `test_shared_vision_quality_changes_encoded_image`, `test_decode_limit_and_bad_limits` | encoder failure message safety-bound; formats configurable; fixture formats/modes test-only |
| Prior attempts and effective warnings | No new operational setting; existing budget/fallback profile values remain in force | `test_fallback_warnings_belong_to_each_attempt`, `test_cloud_budget_exhaustion_reprobes_and_falls_back` | `previous_attempts`, `backend`, `warnings`, `status` artifact-bound record keys; `BUDGET_EXHAUSTED` artifact-bound status |

The changed-line literal scan covered the unstaged runtime diff in `config.py`, `model_client.py`, `model_cloud.py`, `model_image.py`, and `model_secrets.py`; no new hidden operational default was added. The image policy refuses an encoder's incompatible-mode `OSError` with a mode/format diagnostic instead of selecting an unconfigured alpha background. The full allowed format string is validated against installed Pillow encoders; the matrix covers the sole concrete target in repository profiles plus PNG and WEBP, not every encoder Pillow can install. Historical reports below describe earlier snapshots, not the current authentication evidence.

**Mutation evidence (unit, scratch):** `fix1_mutants.py` invoked each pytest via `test_slot.py`, three mutants each for `config`, `model_client`, `model_cloud`, `model_image`, `model_secrets`. Fourteen were killed initially; the byte-preservation mutant survived because an unadorned PNG re-encoded identically. `test_unchanged_image_preserves_original_metadata_bytes` added a metadata-bearing fixture; its mutant retest returned `1 failed in 0.85s` and the unmutated focused run passed. Final count: 15 killed, 0 surviving. The iteration script was removed; individual output logs and scratch copies remain under `.tmp/T-009/fix1-mutants/`: the sandbox policy rejected `Remove-Item` during cleanup, so the PM must clean the worker's scratch tree.

**Additional checks:** image/cloud/config focused run `51 passed, 1 warning in 1.50s`; metadata/link/URL focused run `4 passed, 1 skipped in 1.01s`; exact request-header retest `2 passed in 0.62s`. Expanded image mode/target matrix, with and without resize: `66 passed, 2 warnings in 1.12s`. Final focused image/cloud/secrets/client/config run: `89 passed, 3 skipped, 2 warnings in 2.40s`. Pillow warns that `I`-mode PNG saving is deprecated. The three skipped actual symlink tests cover inside, outside and missing targets and need the PM's unsandboxed run; a synthetic link-check double passed. All tests used the worktree limiter and the project venv with `PYTHONPATH=src`. No live cloud, GPU, Linux, ruff or format check was run. Total request deadline remains out of scope under `Noticed, not done`.

**Full pipeline (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/fix1-suite-final --tb=short` returned `1482 passed, 27 skipped, 1 warning in 476.47s (0:07:56)`. The suite started before the final test-only edits; the final 66-case matrix (including transparency checks) separately returned `66 passed, 2 warnings in 0.77s`, and the other changed tests passed in the focused runs above. No runtime code changed during the suite. The review probe was rerun through `test_slot.py` and gave the same documented outcomes.

**Current status: REVIEWING.** PM: run the three symlink cases outside the sandbox, review the refusal-based image policy and the patch, and clean this worker's scratch trees. The directory-removal attempt was rejected by sandbox policy; the iteration script was deleted. No network, GPU, API call, install or Git write was made in this continuation. Do not repeat the previous failed provider fetches: the PM-supplied quotations above resolve the documented-header question. No new lessons.

## Handoff

### Anthropic workspace follow-up (PM decision pending user review)

`anthropic_workspace_id` has no implicit default. The Anthropic configuration requires a workspace ID or the explicit string `none`; `deploy_required` in the example is an unresolved deployment sentinel, refused when used. The Anthropic request includes `anthropic-workspace-id` only for a concrete ID. The request-header double, loaded-profile override/refusals, and deployment-overlay checks passed: `23 passed in 2.60s`, `62 passed in 64.57s`, and after the loaded-profile-to-header addition `19 passed in 2.40s` through the worktree limiter. `test_selected_anthropic_workspace_sentinel_refused` returned `1 passed in 0.93s` and confirms refusal before a selected role starts. Three scratch mutants each for the overlay allowlist, header construction and schema validation were killed (nine total). The mutation script was removed; its scratch copies and logs remain for PM cleanup because the sandbox previously rejected removal. The review probe was copied to `.tmp/T-009/workspace_review_probe.py`, amended only to specify workspace `none`, and run through the limiter: it exited 0 with the same P1–P4 results recorded above. No live cloud or GPU call was made. The full pipeline suite is being rerun; see the final result below.

**Full pipeline (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/workspace-suite --tb=short` returned `1519 passed, 29 skipped, 2 warnings in 419.09s (0:06:59)`. The later workspace test additions were also run separately as described above; no runtime code changed during this suite. Both warnings are the previously documented Pillow `I`-mode PNG deprecation. `git diff --check` for the follow-up files exited 0 without output. `ruff`/format, Linux, GPU, and live cloud are `not_run`; the three real symlink cases remain skipped in this sandbox.

**Status: REVIEWING.** Changes are in the example profile, configuration schema and overlay allowlist, Anthropic request construction, test fixtures and workspace tests, and this task file. The PM's workspace decision remains pending user review. PM to review, run the three real symlink cases with link rights, and remove this worker's scratch copies; the mutation runner script was deleted. No Git write, install, network, or model call was made. No new lessons.

Review fix round 1 changes `config.py`, `model_client.py`, `model_cloud.py`, `model_image.py`, `model_secrets.py` and their focused tests; details, evidence, mutation results and the configuration audit are in the continuation above. The only sandbox-specific unverified behavior is actual symlink creation/refusal; tests for inside, outside and missing targets are present and skipped without link rights. The project suite and all focused checks passed with the stated skips and Pillow warning. PM cleanup of `.tmp/T-009/` scratch remains necessary; ruff, Linux and live GPU/cloud checks are `not_run`. A total request deadline is outside this round.


## Completion report

### T-006 / T-009 integration (R-015, R-018)

- **Step 0:** The storage lock sentinel can mask model-client checks; combine all three conflict sections, preserve assertions, resolve integration failures, and run the full suite and diff check. No product question arose.
- **Work and reason:** Kept pyarrow with httpx and Pillow; combined the field-contract fixtures and this task's T-009 content. In `tests/test_model_config.py` and `tests/test_model_composition.py`, supplied test-only storage lock values because `kb status` reads `storage.lock` after T-006. Runtime configuration and sentinel enforcement were not changed.
- **Evidence (unit/mock_contract):** With `PYTHONPATH=src`, `PYTHONNOUSERSITE=1`, and `PYTHONDONTWRITEBYTECODE=1`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests` first returned `62 failed, 1380 passed, 26 skipped in 365.82s`; after the fixture fix it returned `1442 passed, 26 skipped in 430.87s`. `git diff --check` returned 0 with `.secrets/README.md: Permission denied` from the sandbox.
- **Configuration audit:** No runtime edits or new operational values in this integration; fixture lock `timeout_s=5`, `poll_s=0.05` is test-only. The existing T-009 audit below remains the model-client handoff evidence.
- **Not run:** GPU/live E12, cloud, Linux, lint, format and mutations; this integration only changed dependency declarations, documentation and test fixtures. No review performed.
- **Status:** TESTING complete for this integration; independent review and the 26 skipped cases remain for the PM. Do not repeat the initial failing suite without the fixture correction. No new lessons.

### Final engine continuation report

**Status: DONE (implementation and CPU/mock-contract checks).** This supersedes the historical blocked/implementing statuses and open hash-gap finding below. R-015/R-018/R-021: profiles validate engine/provider bindings and pin local engine versions; cloud engine versions come from the existing Anthropic setting or Gemini route. Records carry engine identity/version. The canonical request hash includes configured settings before unsupported fields are dropped, resolved output, engine/version and model revision; the effective-body hash is separate. The Gemini route grammar and prior stage ownership remain PM decisions pending user review. Unverified cloud structured output and Gemini image mappings still refuse rather than guessing; live engine support is outside this patch.

**Checks (Windows CPU; labelled doubles):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/engine-suite-final --tb=short` returned `1092 passed, 25 skipped in 265.90s (0:04:25)`. The focused loader/config/client/composition/field-contract set returned `755 passed, 24 skipped in 322.75s`; after the final output-hash change `tests/test_model_client.py` returned `11 passed in 1.88s`. Earlier failing runs and the diagnosis remain recorded below. Ruff lint/format `not_run` (not installed); Linux, E12, GPU and live cloud `not_run` (outside this worker's scope).

**Mutations:** six scratch-copy mutants through `test_slot.py`, all killed: local engine-name validation `1 failed, 5 passed`; cloud name/provider matching `2 failed, 4 passed`; Gemini version-route check `1 failed`; omitted configured sampling, omitted engine identity, and omitted model revision from the hash each `1 failed`. No survivors. `git diff --check -- . ':(exclude).secrets' ':(exclude).secrets/**'` exited 0 with no output; untracked files are not included. The configuration audit below and its engine additions account for each client-read key or name its consumer task; the new-line literal inventory is below.

**Changes and follow-up:** runtime model-client/config modules, enhancer/role schemas, example/dev/vLLM overlay profiles, dependency declaration, and T-009 tests remain unstaged in this worktree. Querying a server for its engine version remains E12 follow-up under `Noticed, not done`; no server, GPU or external cloud request was started. T-006 durable cross-process budget accounting remains a separate integration dependency.

**Cleanup:** `purge_tmp.py archive-remove` removed this continuation's mutation tree and two scratch scripts. It refused protected fake-credential trees `.tmp/T-009/engine-fix-focus`, `.tmp/T-009/engine-client-final`, and `.tmp/T-009/engine-suite-final`; the PM removes them. Earlier protected and historical scratch paths are recorded in the previous reports below.

### Field-contract root cause (before correction)

`tests/test_field_contracts.py:74-113` assumes each example field can be varied alone while the rest of the schema-valid fixture stays fixed; it inventories singleton constraints rather than treating sentinels as alternatives.
The new provider/engine dependency invalidates that fixture assumption: cloud engine names must match the provider and local engines require a version, so a provider or name change needs the corresponding concrete example backend (`configs/enhancer.example.yaml`).
T-005 requires a non-default override per configurable value and refusal of invalid values (`tasks/active/T-005-profile-loader.md:12-13,52`); coherent paired fixture changes retain those assertions, while `deploy_required` must not count as an override.

### Engine continuation: literal inventory and audit

The earlier complete inventory remains applicable to pre-existing code, with line numbers shifted by the engine additions. This table lists every new numeric and non-diagnostic string occurrence in the engine/hash changes; diagnostic message fragments are safety-bound, and docstrings describe code rather than runtime settings. Configuration key lookups are configurable; record and hash member names are artifact-bound (`contracts/vlm-request.md:61`, effective/resolved hash members are the local record extension). Provider and engine names are configurable enum members. The Gemini version syntax is the conservative PM decision above, not a documented provider claim.

| File:line — literal occurrences | Class |
|---|---|
| `config.py:383` `engine` | configurable |
| `config.py:420` `endpoints.chat` | configurable |
| `config.py:421` `/`; `422` `2`, `v`, `0`; `423` `0123456789`, `1` | safety-bound route component structure and indexing |
| `config.py:430` `engine`; `431` `engine`; `432` `provider`, `name` | configurable |
| `config.py:433` `openai_compatible`; `434` `vllm`, `llama_cpp`; `436` `version`; `437` `anthropic`, `gemini`; `438` `anthropic`; `439` and `440` `anthropic_version`; `442` `endpoints`, `chat` | configurable |
| `config.py:445` `name`, `version` | artifact-bound engine identity members, `contracts/vlm-request.md:61` |
| `config.py:485` `provider`; `486` `engine`, `name`; `488` `openai_compatible`, `vllm`, `llama_cpp`; `489` `anthropic`, `gemini`; `491` `endpoints`; `492` `gemini`, `chat`; `493` `chat` | configurable |
| `model_client.py:138` `vlm.backends.`; `197` `vlm.backends.` | safety-bound diagnostic context |
| `model_client.py:177` `output`; `288` `image`; `291` `vision`; `292` `max_image_side`, `format`; `293` `max_image_side`, `max_side`, `from_vision`, `from_vision`; `294` `format`, `format`, `from_vision` | configurable |
| `model_client.py:295` `body_hash`, `role`, `prompt_id`; `296` `image_roles`; `297` `image`, `vision`, `sampling`; `298` `reasoning`, `output`; `299` `resolved_output`, `engine`; `300` `model_revision` | artifact-bound canonical hash members, `contracts/vlm-request.md:61` |
| `model_client.py:295` `prompt_id`; `297` `sampling`; `298` `reasoning`; `300` `model`, `revision` | configurable source key lookups |
| `model_client.py:302` `engine` | artifact-bound call record, `contracts/vlm-request.md:61` |

| Configuration | Override/effect and refusal |
|---|---|
| `engine.name`, local `engine.version` | `test_engine_profile_override_and_refusal` and `test_loaded_profile_composes_role_client` check non-default profiles and loopback record; `test_model_backend_read_refuses_operational_sentinel` refuses unresolved values. |
| `anthropic_version` | `test_cloud_requires_ledger_and_uses_synthetic_key` checks synthetic header and record; `test_anthropic_version_override_and_refusal` checks invalid configuration. |
| Gemini `endpoints.chat` | `test_gemini_engine_version_is_taken_from_route` checks version extraction and refusal; `test_gemini_text_response_and_usage_with_synthetic_key` checks the record with an in-memory double. |
| Request hash inputs | `test_request_hash_includes_dropped_settings_and_engine` checks differing dropped sampling, configured output, engine name/version and revision; prior configuration audit and tests below cover the remaining individual inputs and refusals. |

### Engine identity continuation: BLOCKED

**Status: BLOCKED.** The same `tests/test_field_contracts.py::test_protocol_only_fields` check failed twice; footer rule 17 requires stopping. The first failure was the new cross-field engine/provider constraint making the existing generic fixture unable to select a valid provider alternative. A fixture revision then included `deploy_required` among generic candidates, causing the second failure by making four protocol-only fields appear to have alternatives. The fixture was revised again after those failures, but that revision is **not verified**. Finish by running the focused checks, fixing any remaining fixture or product defects, running mutations and the full suite, and updating the complete literal inventory and audit before considering `DONE`.

**Changes:** Added schema/profile engine identity, provider matching and version resolution, call-record engine metadata, and canonical request hashing that includes pre-drop settings plus engine/version and model revision. Gemini route version is conservatively read from its first version-shaped component; this is a PM decision pending user review. Added loader, direct-double and loopback composition tests, but the latest test changes are unverified. Server-version discovery is out of scope and remains E12 follow-up.

**Evidence (unit/mock_contract):** limiter command with `tests/test_model_config.py -k engine` returned `8 passed, 110 deselected in 7.50s`; limiter command with `tests/test_model_client.py -k request_hash` returned `1 passed, 10 deselected in 1.44s`. Earlier broad focused command returned `7 failed, 734 passed, 26 skipped in 449.31s`; failures were `test_protocol_only_fields`, `test_all_constrained_fields_are_known`, engine/provider fixture overrides and the Gemini route fixture. A separate `tests/test_field_contracts.py -x` run returned `1 failed in 28.98s` for the same `test_protocol_only_fields` check after the intermediate fixture change. The engine-name and provider fixture corrections are unverified; Gemini endpoint fixture still needs correction. A full-suite attempt was interrupted before completing, so the current full suite is **not_run**. Engine continuation mutants and refreshed per-occurrence literal inventory are **not_run**. `git diff --check -- . ':(exclude).secrets' ':(exclude).secrets/**'` exited 0 before the last test edits; untracked files are excluded. Ruff is absent; lint/format, Linux, E12, GPU and live cloud calls are **not_run**.

**Configuration audit additions, incomplete:** `engine.name` and local `engine.version` are configurable; `test_engine_profile_override_and_refusal` verifies loader overrides/refusals and `test_model_backend_read_refuses_operational_sentinel` now includes both sentinels but its new cases are **not_run**. `test_loaded_profile_composes_role_client` now includes name/version loopback cases but those cases are **not_run**. `test_gemini_engine_version_is_taken_from_route` verifies route-derived version and invalid-route refusal; Anthropic record-version effect remains **not_run**. `test_request_hash_includes_dropped_settings_and_engine` verifies the dropped sampling value, engine name/version and model-revision hash changes against a labelled in-memory double; full individual configured-field coverage remains incomplete. Prior audit rows remain as recorded below; new literals and changed line references need classification.

**Scratch:** Existing protected synthetic-credential trees `.tmp/T-009/parallel-suite-current`, `.tmp/T-009/parallel-suite-final`, `.tmp/T-009/parallel-validation-focus`, `.tmp/T-009/pillow-validation-focus`, `.tmp/T-009/profile-effects-cloud` remain for PM removal. This continuation also created `.tmp/T-009/engine-focus`, `.tmp/T-009/engine-specific`, `.tmp/T-009/engine-hash`, `.tmp/T-009/engine-fields` and possibly `.tmp/T-009/engine-suite`; leave them for PM cleanup. No model server, GPU or cloud call was started.

### Engine identity continuation: Step 0 and configuration impact

R-015/R-018/R-021: PM decision pending user review authorizes `vlm.backends.<backend>.engine.name` and local-only `engine.version`. Cloud versions reuse `anthropic_version` and the versioned Gemini chat endpoint; no duplicate cloud version setting. Unknown: the precise spelling of a version-bearing Gemini route. Conservative structural handling is to use its first nonempty relative route component and refuse a route that does not carry a version component, without querying a server; this is pending PM review. Canonical request hashing includes original role settings, image settings, selected engine identity/version and model revision before optional fields are dropped; the effective-body hash stays separate. Check configured effect, mismatch and sentinel refusals, provider-specific fallback, hash divergence and deterministic serialization, then mutations and full suite. No cloud call, GPU, model server or server version query.

| Configuration | Schema / examples | Override / refusal planned | Class |
|---|---|---|---|
| `vlm.backends.*.engine.name` | `enhancer_config.v0` backend engine object; local `vllm` / `llama_cpp` or `deploy_required`, cloud name exactly provider. Base example `deploy_required` for local, concrete provider name for cloud; dev `llama_cpp`, vLLM deployment overlay `vllm`. | Load local override and reject invalid/provider mismatch; effect recorded by loopback test. | configurable |
| `vlm.backends.*.engine.version` | `enhancer_config.v0`: local nonempty string or `pin_required`; base/dev/overlay `pin_required`; cloud uses existing provider API version field/route, without duplicate key. | Load non-default pin, reject missing/invalid and selected sentinel; check record/hash effect. | configurable |
| `anthropic_version`, Gemini `endpoints.chat` | Existing schema/example sentinels and route validation; selected provider supplies the record API version. | Verify synthetic Anthropic version and versioned Gemini route in records; reject mismatch/unresolved route. | configurable; route parsing safety-bound |
| `vlm.roles.*.{image,sampling,reasoning,output}`, `prompt_id`, backend `model.revision` | Existing role/config schemas and examples; included as configured values in canonical request hash before drop. | Loopback/direct-double tests vary fields independently, including unsupported sampling, and verify distinct hashes. Existing invalid-value refusal tests apply. | configurable; canonical JSON mechanics artifact-bound |

### Parallel decode continuation: Step 0 and configuration impact

R-015/R-017/R-024: replace per-call Pillow global mutation with per-image size refusal before decoding; a composition-time process-wide Pillow bomb backstop may only increase to the largest concrete configured backend decode limit. Concurrent roles use their own configured `vlm.backends.<name>.image.max_decode_pixels`; existing `enhancer_config.v0` schema and example/dev/deployment profiles use positive integers or `deploy_required`. Non-default and invalid/refusal evidence: `test_decode_limit_and_bad_limits`, a concurrent different-limits test, and composition-level backstop override tests. Sentinel backend limits remain refused before use; no new config value or default is introduced. `output.validate_response` belongs to `vlm_role.v0` and the nested enhancer schema; both require true for structured output (`contracts/vlm-request.md:12-13`). Preserve that contract and exercise false on unstructured text; a schema-bearing unstructured response may be validated against its explicitly configured schema. Read output fields explicitly and test effects and refusals. Other configuration impact remains the composition continuation table; no GPU, external network, paid call, new dependency or deployment change.

### Parallel decode continuation: completion report

**Work completed:** `model_image.py` checks each opened image's dimensions before `load` or EXIF processing, never mutating Pillow's global per call. `configure_decode_backstop` only raises the process-wide Pillow threshold, under a lock, to the largest concrete configured backend limit when a run is composed; Pillow's own warning/error remains an additional backstop. `model_client.py` reads `output.validate_response`, requires it for structured output, and applies an explicitly named schema to text when validation is enabled. `test_model_image.py`, `test_model_client.py`, and the profile-driven loopback table in `test_model_composition.py` exercise the changes and the configuration effects below. No model server, GPU, real provider or paid call was started.

**Unit/mock_contract evidence:** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/parallel-suite-current --tb=short` returned `1075 passed, 25 skipped in 226.58s (0:03:46)`. Focused: concurrent image suite `7 passed in 0.79s`; profile transport `7 passed, 33 deselected in 8.60s`; output `5 passed, 38 deselected in 6.12s`; image `6 passed, 43 deselected in 7.13s`; cloud `4 passed, 47 deselected in 5.47s`; writer `1 passed, 51 deselected in 2.04s`; concurrency `1 passed, 51 deselected in 1.97s`. `ruff` is absent; lint/format `not_run`; Linux, E12, GPU and live cloud `not_run` by scope.

**Mutations (scratch, limiter, unit/mock_contract):** three each in `model_image.py` and `model_client.py` were killed: removing the image size check (`1 failed, 68 deselected`), suppressing or reversing the backstop increase (`1 failed, 68 deselected` each), bypassing structured-output refusal (`1 failed, 68 deselected`), bypassing configured schema validation (`1 failed, 68 deselected`), and omitting composition backstop setup (`1 failed, 1 passed, 67 deselected`). The concurrent image test uses two threads with independent limits and an opening barrier; the first mutant specifically proves its refusal assertion detects removal of the size check.

**Configuration audit:** each row is configurable unless noted. `test_loaded_profile_composes_role_client` is the parameterized profile → composition → bounded loopback double test; its invalid/refusal companions are named explicitly. The user-directed consumer assignments are PM decisions pending user review. The following supersedes the older incomplete configuration tables for the paths listed.

| Configuration path | Non-default effect / refusal evidence |
|---|---|
| `vlm.default_backend`, role `backend` | `test_cloud_budget_is_shared_by_roles_from_same_run` exercises bound cloud and local fallback; `test_binding_rules_through_profile_load`, `test_role_values_through_command_load` refuse bad binding. |
| backend `provider` | `test_cloud_budget_is_shared_by_roles_from_same_run` and `test_loaded_profile_composes_role_client` exercise cloud/local; `test_backend_enum_refused` refuses an invalid provider. |
| backend `base_urls`, `endpoints.chat` | `test_loaded_profile_composes_role_client` observes alternate path and URL; `test_config_values_through_profile_load`, `test_unsafe_url_refused_at_profile_load`, `test_endpoint_missing_is_refused` refuse invalid values. |
| `endpoints.health` | Consumer: T-016; T-009 does not read it. |
| `api_key_file`, `anthropic_version` | `test_cloud_budget_is_shared_by_roles_from_same_run` checks synthetic authorization and version headers; `test_config_values_through_profile_load`, `test_anthropic_version_override_and_refusal`, `test_synthetic_credential_and_path_refusal` refuse invalid input. |
| `model.id`, `model.revision` | `test_loaded_profile_composes_role_client` observes request and record; `test_binding_rules_through_profile_load`, `test_config_values_through_profile_load` refuse invalid values. |
| `model.source`, `model.file`, `model.mmproj_file` | Consumer: T-016; T-009 does not read them. |
| `max_concurrency` | `test_loaded_profile_composes_role_client` observes at most three concurrent loopback calls; `test_config_values_through_profile_load`, `test_zero_concurrency_refused` refuse zero. |
| `call_budget`, `on_budget_exhausted`, `allow_images` | `test_cloud_budget_is_shared_by_roles_from_same_run` observes shared budget, stop/fallback, and local/cloud image choices; `test_config_values_through_profile_load` refuses invalid settings. |
| `unsupported_param_policy` | Only `warn` is allowed, so a non-default override does not apply; `test_loaded_profile_composes_role_client` observes unsupported-field warning and `test_backend_policy_refused` refuses other policies. |
| `extra_body` | `test_loaded_profile_composes_role_client` observes the merged field; `test_config_values_through_profile_load` refuses wrong type. |
| `http.connect_timeout_s`, `read_timeout_s`, `write_timeout_s`, `pool_timeout_s` | `test_loaded_profile_composes_role_client` observes each timeout on the live transport; `test_backend_operational_schema` refuses missing/invalid values and `test_nonfinite_and_backoff_order_refused` covers nonfinite. |
| `http.max_retries`, `retry_statuses`, `backoff_initial_s`, `backoff_max_s` | `test_loaded_profile_composes_role_client` observes retry count/status and captured delays; `test_backend_operational_schema`, `test_nonfinite_and_backoff_order_refused` refuse invalid values/order. |
| `probe.max_requests`, `probe.timeout_s` | `test_loaded_profile_composes_role_client` observes request count and timeout failure; `test_backend_operational_schema` refuses invalid/missing values. |
| backend `image.max_decode_pixels` | `test_loaded_profile_composes_role_client` observes backstop and refuses oversized input; `test_concurrent_images_use_independent_limits` checks two concurrent per-call limits; `test_backend_operational_schema` refuses invalid/missing values. |
| role `mode` | Only `generate` is accepted, so a non-default override does not apply; `test_role_mode_refused` checks refusal. |
| role `prompt_id`, `max_tokens` | `test_loaded_profile_composes_role_client` observes record/request; `test_role_values_through_command_load` refuses invalid values. |
| role sampling `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `stop` | `test_loaded_profile_composes_role_client` observes each mapped field or the required unsupported warning; `test_role_values_through_command_load`, `test_nonfinite_sampling_refused` refuse invalid input. |
| role reasoning `mode`, `effort`, `budget_tokens`, `template_kwargs_key` | `test_loaded_profile_composes_role_client` observes mapped values or unsupported warning; `test_role_values_through_command_load`, `test_binding_rules_through_profile_load` refuse invalid values. |
| role `image.max_side`, `format`, `quality`; `vision.max_image_side`, `format` | `test_loaded_profile_composes_role_client` observes encoded image size, format and JPEG quality; `test_role_values_through_command_load`, `test_config_values_through_profile_load`, `test_image_members_required_and_max_side_can_inherit` refuse invalid/missing values. |
| `vision.quality` | `test_loaded_vision_quality_changes_shared_encoding` observes two encodings from loaded profiles and refuses invalid quality; `test_shared_vision_quality_changes_encoded_image` refuses its unresolved sentinel. |
| role image `source`, `transport` | Consumers T-010/T-014; T-009 does not read them. |
| `vision.transport` | Consumer T-008; T-009 does not read it. |
| output `format`, `schema_id`, `strict`, `validate_response` | `test_loaded_profile_composes_role_client` observes output format/schema/strict, writer manifest resolution and both validation choices; `test_role_values_through_command_load`, `test_role_validation_override_without_schema`, `test_structured_output_without_validation_is_refused` exercise refusals. Structured output cannot disable validation. |
| output `transport` | Consumer T-015; T-009 does not read it. |
| storage, device and cross-run persistence | Consumers T-006/T-016; T-009's single-run ledger is not a durable cross-process ledger. Transport close and cancellation are covered by `test_transport_timeout_and_cancellation_release_concurrency`. |

**Open record issue:** `contracts/vlm-request.md:61` requires engine and engine version in the call record and request hash, as well as image settings, sampling, reasoning and output settings. `model_client.py` records provider, backend and model revision, but no separate engine identity/version; the existing profile schema has no such configured fields. Its hash includes the effective body and selected metadata, but omits configured values dropped as unsupported and image settings that leave the body unchanged. This pre-existing gap is not proof of engine behavior or E12; it remains an implementation/contract issue for PM review and prevents asserting that the complete T-009 goal is finished. No new identity or version was guessed.

**Hash-gap experiment (unit, labelled in-memory double):** the same limiter and Python prefix with `.tmp/T-009/test_hash_gap.py --basetemp=.tmp/T-009/hash-gap-check --tb=short` returned `1 passed in 0.73s`: `repetition_penalty` 1.2 versus 1.3 is warned/dropped in both calls and produces an identical `request_hash`. The test documents the existing defect; it is not a passing acceptance test. The configured engine identity/version source and any new keys need PM direction under the brief's `Must ask` rule.

**Status: IMPLEMENTING, incomplete:** requested decode, output-validation, configuration evidence and per-occurrence inventory work is recorded above; the open call-record/hash gap prevents `DONE`. `git diff --check -- . ':(exclude).secrets' ':(exclude).secrets/**'` exited 0; it excludes the denied credential tree and, like ordinary `git diff`, does not check untracked files. The project `purge_tmp.py archive-remove` removed this continuation's scripts and most scratch. It refused five trees because their synthetic test credential directories are protected: `.tmp/T-009/parallel-suite-current`, `parallel-suite-final`, `parallel-validation-focus`, `pillow-validation-focus`, and `profile-effects-cloud`; PM cleanup is required. Earlier continuation scratch remains as previously reported.

### Composition continuation: Step 0 and configuration impact

R-015/R-018/R-021. Unknowns: resource ownership across roles and precise image-quality precedence; preserve the existing explicit per-role `image.quality` for role calls and expose configured `vision.quality` for the shared image-encoding path. A run must reuse one budget ledger and one concurrency guard across all of its roles, and unused backends may retain unresolved deployment sentinels. Plan: connect loaded profile to run-scoped resources and role-bound clients, exercise effects and refusals through profile load and bounded HTTP doubles, set and restore the Pillow decode bound without losing the configured limit, classify literals, mutate wiring, run the suite. Finish only with audited effects and ownership.

**Configuration impact:** `enhancer_config.v0` and `vlm_role.v0`; existing `configs/enhancer.example.yaml`, dev and deployment overlays, with existing sentinels and no new keys. Affected configurable paths: `vlm.default_backend`, role `backend`, `prompt_id`, `max_tokens`, `sampling.*`, `reasoning.*`, `image.{max_side,format,quality}`, `output.{format,schema_id,strict,validate_response}`; backend `provider`, `base_urls`, `endpoints.chat`, `api_key_file`, `anthropic_version`, `model.{id,revision}`, `max_concurrency`, `call_budget`, `on_budget_exhausted`, `allow_images`, `extra_body`, `http.*`, `probe.*`, `image.max_decode_pixels`; `vision.{max_image_side,format,quality}`. `vision.transport` consumer T-008 (PM decision pending user review); image source/transport and output transport retain the prior stage ownership. All are configurable; example deployments must resolve applicable sentinels before use. Profile-load override/refusal and client-visible boundary tests are required for each applicable value, with individual gaps reported. No model, GPU, new dependency, network call or paid request is authorized; the in-process ledger is one-run only.

**Implementation and evidence so far:** `compose_model_client` creates a role-bound client with a run-owned ledger, concurrency guard, transports, schema registry and probes; `for_role` shares that run, and `close` closes its transports. No product caller existed in `src/` before this change; direct construction remains in labelled unit tests. `prepare_vision_image` applies `vision.quality` to the shared encoding path, while explicit role quality remains the role override. The Pillow opening limit is temporarily set to configured `max_decode_pixels` and restored under a lock used by this module; this lock cannot coordinate unrelated Pillow users elsewhere in the process, an open integration risk. Installed Pillow `Image.py:3438-3455` uses `MAX_IMAGE_PIXELS` for warning and twice that limit for error; `ImageOps.py:686-700` identifies Orientation via `ExifTags.Base.Orientation` and `ExifTags.py:41` defines `Orientation = 0x0112` (274). Pillow's primary documentation is https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose . `vision.transport` is assigned to T-008 per PM decision pending user review.

**New profile-to-effect evidence (unit / loopback mock_contract):** `test_loaded_profile_composes_role_client` observes `model.id`, `model.revision`, `endpoints.chat`, all four `http.*_timeout_s`, `max_concurrency`, `extra_body`, role `max_tokens`, `prompt_id`, and `sampling.temperature`; invalid values are refused by `test_backend_operational_schema`, `test_config_values_through_profile_load`, `test_role_values_through_command_load`, and `test_unsafe_url_refused_at_profile_load` as applicable. `test_cloud_budget_is_shared_by_roles_from_same_run` exercises `call_budget`, `on_budget_exhausted`, default backend, cloud authentication, fallback and reprobe using a synthetic key; schema refusal is in `test_config_values_through_profile_load`. `test_loaded_vision_quality_changes_shared_encoding` loads two qualities and verifies changed encoding and invalid refusal. `test_configured_decode_bound_restores_pillow_limit` tests a configured override, refusal and restoration. Other earlier audit rows remain open; these tests do not establish their effects. The run object must be reused for all roles in a single run; independently composing two runs creates two in-process ledgers and does not protect a grant shared across runs (durable accounting is T-006).

The same table test additionally observes role sampling `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`, and a `PARAM_UNSUPPORTED` warning for `repetition_penalty`; it observes reasoning `mode`, `effort`, `template_kwargs_key` and the warning for unsupported `budget_tokens`. `test_role_values_through_command_load` supplies their override and invalid-value refusal cases. `test_composition_refuses_unresolved_inherited_vision` refuses unresolved inherited size, format or whole vision block. Per-profile runtime evidence remains incomplete for retry/backoff/status policies, probe limits, profile-to-image decode limit/format/size/role quality, cloud version/allow-images, output/schema/strict and some remaining behavior: the existence of wiring alone is not proof of each effect.

**Current result:** `tests/test_model_composition.py -k inherited_vision` returned `3 passed, 27 deselected in 1.88s`; the preceding expanded composition run returned `27 passed in 28.16s`; the final full suite command was `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src <project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/composition-suite-final --tb=short`, returning `1050 passed, 25 skipped in 194.76s (0:03:14)` (unit / loopback mock_contract). Two intermediate full runs were interrupted after later code changes made them stale; an earlier completed pre-final run returned `1035 passed, 25 skipped in 209.08s`. Three scratch mutants, run through the limiter against `tests/test_model_composition.py`, were killed: hardcoded concurrency (`1 failed, 14 passed`), zero cloud budgets (`2 failed, 13 passed`), and probing the default rather than bound backend (`2 failed, 13 passed`). These mutations precede the added inherited-vision checks; they do not test those new lines. Ruff and Linux are `not_run` (ruff absent, Linux unavailable); E12, GPU and live clouds are `not_run` by scope.

**Status: IMPLEMENTING, incomplete.** Although numeric literals are inventoried, non-message string classification remains incomplete, especially `config.py`; not every applicable profile-to-runtime effect or refusal has a matching test. The new composition requires consumers to keep one run per grant; no product caller exists yet. The Pillow global-limit interaction with unrelated threads is an open integration risk. The two scratch scripts created for this continuation were removed; `Remove-Item -LiteralPath .tmp/T-009/composition-budget -Recurse -Force` was rejected by command policy before execution. The `composition-*` pytest and mutation trees remain under `.tmp/T-009/` for PM cleanup. No model server, GPU or external request was started.

**Numeric literal inventory:** AST scan of the changed `config.py` and all nine new `model_*.py` files, including repeated occurrences by line (booleans are not numbers in this table). Index arithmetic, cardinalities and positive limits listed as safety-bound are fixed structural/safety checks, not user-tunable settings. The protocol bound is from the cited prior scout and PM decision, with the research limitation above.

| File:line = literal | Class and purpose |
|---|---|
| `config.py:17=128` | safety-bound schema-cache capacity. |
| `config.py:72=0,0,1,2`; `74=0,1`; `85=0`; `97=0`; `105=8`; `107=4`; `193=0`; `227=0,2`; `237=0` | safety-bound path structure, link spelling and boundary indices. |
| `config.py:179=2`; `319=0`; `323=0`; `373=0`; `386=0` | safety-bound diagnostic selection/formatting indices. |
| `config.py:345=1`; `532=1`; `604=1` | artifact-bound schema version. |
| `config.py:500=1024` | protocol-bound Anthropic thinking minimum, https://platform.claude.com/docs/en/api/http/messages/create. |
| `config.py:523=2`; `600=2`; `622=2` | artifact-bound package-to-project-root layout. |
| `model_client.py:117=0`; `142=2`; `143=2`; `146=2`; `218=1`; `314=0`; `324=0` | safety-bound nonnegative usage, paired challenge counts, image count and first-response indexing. |
| `model_cloud.py:29=1024` | protocol-bound Anthropic thinking minimum, https://platform.claude.com/docs/en/api/http/messages/create. |
| `model_image.py:26=1`; `28=1`; `51=1,1` | safety-bound positive limit and minimum resized dimension. |
| `model_image.py:43=274,1`; `53=1` | protocol-bound EXIF Orientation tag and normal orientation, https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose; installed Pillow source inspected above. |
| `model_probe.py:25=1`; `26=0`; `75=0,0`; `76=1,1`; `77=0,1`; `83=1`; `85=0`; `86=0`; `87=0,0,1,1`; `93=0`; `102=0,0,0,1` | safety-bound probe limits, cardinalities and paired-challenge indexing. |
| `model_resources.py:23=0`; `33=0`; `35=1`; `54=1`; `62=1` | safety-bound budget and concurrency invariants and unit reservation. |
| `model_secrets.py:9=0,2`; `11=1`; `17=1` | safety-bound credential path component cardinality and indices. |
| `model_transport.py:47=0`; `49=1`; `65=2` | safety-bound first configured base, initial attempt count and exponential retry arithmetic; backoff magnitudes remain configured. |

`model_mapping.py` and `model_schemas.py` contain no numeric AST constants. This completes the numeric pass only; the requested classification of every non-message string is still open. Literal categories used for config keys, documented provider wire names, diagnostics and call-record vocabulary must be verified against actual usage and contract/source lines, not assigned solely from spelling.

**String groups:** call-record and contract vocabulary is artifact-bound against `contracts/vlm-request.md:9-14,22,59-61`; provider request/response vocabulary is protocol-bound to the individual provider URLs in `.tmp/T-009/provider-docs.md`; error-message text is safety-bound diagnostic text. This grouping does not by itself account for every non-message string or resolve the unverified Gemini authentication caveat. Individual shared-image string literals are:

| File:line | Literal | Class |
|---|---|---|
| `model_image.py:16,20` | `max_image_side` | configurable `vision.max_image_side` key. |
| `model_image.py:16,21` | `format` | configurable `vision.format` key. |
| `model_image.py:16,21` | `quality` | configurable `vision.quality` key. |
| `model_image.py:26,49` | `none` | configurable enum value for `vision.max_image_side` / role override. |
| `model_image.py:31` | `error` | safety-bound warning filter action. |
| `model_image.py:58` | `quality` | protocol-bound Pillow encoder option, https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg . |

Function/module docstrings are documentation rather than runtime operational values. The other changed files' non-message strings still need individual classification; this table is not the complete requested inventory.

Additional individual non-message strings (diagnostic text remains in the safety-bound group above):

| File:line | Literal | Class |
|---|---|---|
| `model_resources.py:16` | `reserved` | safety-bound reservation state. |
| `model_resources.py:48` | `committed` | safety-bound reservation state. |
| `model_resources.py:55` | `released` | safety-bound reservation state. |
| `model_schemas.py:15` | `*.schema.json` | artifact-bound local file convention. |
| `model_schemas.py:19` | `$id` | protocol-bound JSON Schema identifier, https://json-schema.org/draft/2020-12/json-schema-core#section-8.2.1 . |
| `model_schemas.py:20` | `.schema.json` | artifact-bound local file convention. |
| `model_secrets.py:9,14,15` | `.secrets` | safety-bound credential directory boundary. |
| `model_secrets.py:11` | `\\`, `.`, `..` | safety-bound path traversal rejection. |
| `model_secrets.py:20` | `utf-8` | safety-bound credential decoding. |
| `model_secrets.py:21` | `\n`, `\r` | safety-bound single-line credential check. |
| `model_transport.py:14,23` | `http`, `https` | protocol-bound URL scheme vocabulary, https://www.rfc-editor.org/rfc/rfc9110#section-4.2 . |
| `model_transport.py:16,17,25` | `/`, `.`, `..`, `\\`, `%`, empty string | safety-bound URL and route boundary/assembly tokens. |
| `model_transport.py:22` | `localhost` | safety-bound loopback policy. |
| `model_transport.py:34,51` | `provider`, `openai_compatible` | configurable backend provider key and local-provider enum. |
| `model_transport.py:36,46` | `http` | configurable backend HTTP section key. |
| `model_transport.py:37` | `connect_timeout_s`, `read_timeout_s` | configurable backend timeout keys. |
| `model_transport.py:38` | `write_timeout_s`, `pool_timeout_s` | configurable backend timeout keys. |
| `model_transport.py:47` | `base_urls` | configurable backend URL key. |
| `model_transport.py:49,60,63` | `max_retries` | configurable backend retry bound key. |
| `model_transport.py:63` | `retry_statuses` | configurable backend retry status key. |
| `model_transport.py:65` | `backoff_initial_s`, `backoff_max_s` | configurable backend retry backoff keys. |
| `model_cloud.py:12` | `api_key_file` | configurable backend credential path key. |
| `model_cloud.py:13,18,26,38,46` | `anthropic`, `gemini` | configurable provider enum values. |
| `model_cloud.py:14` | `anthropic_version` | configurable version key. |
| `model_cloud.py:15` | `deploy_required` | configurable unresolved deployment sentinel. |
| `model_cloud.py:17` | `Authorization`, `Bearer `, `anthropic-version` | protocol-bound, https://platform.claude.com/docs/en/api/overview . |
| `model_cloud.py:20` | `x-goog-api-key` | protocol-bound on interactions endpoint only, https://aistudio.google.com/docs/api-key; generateContent unverified. |
| `model_cloud.py:26,27,29` | `reasoning`, `mode`, `on`, `budget_tokens`, `max_tokens` | configurable role keys/enum. |
| `model_cloud.py:34,35` | `output`, `format`, `none` | configurable output keys/enum. |
| `model_cloud.py:40,42,43,44,45` | `type`, `text`, `image`, `source`, `base64`, `media_type`, `data`, `model`, `system`, `messages`, `role`, `user`, `content` | protocol-bound Anthropic request members, https://platform.claude.com/docs/en/api/http/messages/create . |
| `model_cloud.py:43` | `ascii` | safety-bound base64 text decoding. |
| `model_cloud.py:44` | `model`, `id` | configurable backend model key and identifier key. |
| `model_cloud.py:50-52` | `systemInstruction`, `parts`, `text`, `contents`, `role`, `user`, `generationConfig` | protocol-bound Gemini request members, https://aistudio.google.com/api-reference/generate-content . |
| `model_cloud.py:55` | `extra_body` | configurable backend extra-body key. |
| `model_mapping.py:8-14` | provider-map keys `openai_compatible`, `anthropic`, `gemini`, plus source sampling keys `temperature`, `top_p`, `top_k`, `presence_penalty`, `frequency_penalty`, `seed`, `stop` | configurable provider/role keys. |
| `model_mapping.py:8-14` | mapping outputs `temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`, `stop_sequences`, `topP`, `topK`, `presencePenalty`, `frequencyPenalty`, `stopSequences` | protocol-bound to https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create , https://platform.claude.com/docs/en/api/http/messages/create , https://aistudio.google.com/api-reference/generate-content . |
| `model_mapping.py:22,24,28,29,30,31,33,44,47-49,51,53,55,58,61,63` | `openai_compatible`, `top_k`, `min_p`, `anthropic`, `gemini`, `max_tokens`, `sampling`, `none`, `mode`, `effort`, `budget_tokens`, `on`, `auto`, `backend_default` | configurable provider and role keys/enums. |
| `model_mapping.py:25,34,35,52,56,59` | `sampling.`, `PARAM_UNSUPPORTED`, `reasoning.mode`, `reasoning.budget_tokens` | artifact-bound warning vocabulary, `contracts/vlm-request.md:9-11`. |
| `model_mapping.py:28` | `max_tokens`, `maxOutputTokens` mapping outputs | protocol-bound, https://platform.claude.com/docs/en/api/http/messages/create and https://aistudio.google.com/api-reference/generate-content . |
| `model_mapping.py:50,54` | `thinking`, `type`, `enabled`, `budget_tokens`, `output_config`, `effort` | protocol-bound, https://platform.claude.com/docs/en/api/http/messages/create . |
| `model_mapping.py:62,64,66` | `thinkingLevel`, `thinkingBudget`, `thinkingConfig` | protocol-bound, https://aistudio.google.com/api-reference/generate-content . |
| `model_probe.py:41,104,106,134,136` | `failed`, `incomplete`, `complete` | artifact-bound capability result states. |
| `model_probe.py:48` | `, ` | safety-bound diagnostic separator. |
| `model_probe.py:81-86,93-96` | `messages`, `content`, `type`, `image_url`, `url`, `choices`, `finish_reason`, `stop`, `message`, `refusal` | protocol-bound, https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create and https://developers.openai.com/api/reference/resources/chat . |
| `model_probe.py:86` | `<image>` | safety-bound comparison placeholder, never sent. |
| `model_probe.py:97` | `value` | artifact-bound caller-supplied probe-result structure. |
| `model_probe.py:102` | `first`, `second` | safety-bound distinct challenge labels. |
| `model_probe.py:106` | `image` | artifact-bound capability name. |
| `model_probe.py:117,118,120` | `chat_template_kwargs` | protocol-bound, https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md . |
| `model_probe.py:132,133` | `on`, `off` | safety-bound distinct challenge labels. |
| `model_probe.py:136` | `reasoning.mode` | artifact-bound capability name, `contracts/vlm-request.md:11`. |
| `model_client.py:33-69` | `vlm`, `backends`, `roles`, `max_concurrency`, `call_budget`, `image`, `vision`, `max_side`, `from_vision`, `max_image_side`, `format`, `default_backend`, `backend`, `on_budget_exhausted`, `fallback_to_default_backend` | configurable profile keys/enums. |
| `model_client.py:41` | `contracts`, `schemas` | artifact-bound project schema directory. |
| `model_client.py:48,55,57,59,62` | `vlm.roles.`, `vision.max_image_side`, `vision.format`, `vlm.default_backend`, `vlm.backends.` | safety-bound refusal path labels. |
| `model_client.py:106,107` | `,`, `:`, `utf-8` | artifact-bound canonical request-hash serialization. |
| `model_client.py:111-116` | `usageMetadata`, `usage`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `input_tokens`, `output_tokens`, `promptTokenCount`, `candidatesTokenCount` | protocol-bound response fields, https://developers.openai.com/api/reference/resources/chat , https://platform.claude.com/docs/en/api/http/messages/create , https://aistudio.google.com/api-reference/generate-content . |
| `model_client.py:111-116,136-142,173,183,185,214,225,235,240,247-248,250,253,258,260,263,265,270,272` | `gemini`, `openai_compatible`, `anthropic`, `provider`, `probe`, `chat`, `endpoints`, `max_requests`, `timeout_s`, `writer`, `none`, `allow_images`, `output`, `model`, `id`, `quality`, `max_decode_pixels`, `reasoning`, `mode`, `on`, `off`, `template_kwargs_key`, `effort`, `backend_default`, `budget_tokens`, `schema_id`, `json_schema`, `json_object`, `strict`, `extra_body` | configurable profile keys and enum values. |
| `model_client.py:145,159` | `thinking`, `ledger` | safety-bound run probe-plan/transport interface members. |
| `model_client.py:148-150,219,249,254,259` | `incomplete`, `failed`, `complete`, `multiple_images`, `reasoning.mode`, `reasoning.effort`, `reasoning.budget_tokens` | artifact-bound probe results/capability names. |
| `model_client.py:210,252,346` | `BUDGET_EXHAUSTED`, `PARAM_UNSUPPORTED`, `SCHEMA_INVALID` | artifact-bound warnings, `contracts/vlm-request.md:10,13,22`. |
| `model_client.py:236,243-246,250,255,266-270` | `type`, `text`, `image/`, `image_url`, `url`, `data:image/`, `;base64,`, `messages`, `role`, `content`, `system`, `user`, `chat_template_kwargs`, `reasoning_effort`, `response_format`, `json_object`, `json_schema`, `name`, `schema`, `strict` | protocol-bound provider request fields, https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create and https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md . |
| `model_client.py:238` | `source_image`, `user_reference`, `knowledge_reference` | artifact-bound image-role labels, `contracts/vlm-request.md:24-34,55-57`. |
| `model_client.py:242` | `ascii` | safety-bound base64 encoding. |
| `model_client.py:278-287,302-309,330-343,346` | `body_hash`, `prompt_id`, `model_revision`, `schema_hash`, `image_roles`, `model_id`, `effective_body_hash`, `request_hash`, `warnings`, `HTTP_ERROR`, `status`, `error_type`, `latency_s`, `http_status`, `INCOMPLETE_OR_REFUSED`, `other`, `OK`, `SCHEMA_INVALID` | artifact-bound call-record keys/statuses, `contracts/vlm-request.md:13,59-61`. |
| `model_client.py:314-332` | `choices`, `message`, `finish_reason`, `refusal`, `stop`, `stop_reason`, `end_turn`, `stop_sequence`, `candidates`, `finishReason`, `parts`, `SAFETY`, `STOP`, `length`, `content_filter`, `max_tokens`, `MAX_TOKENS` | protocol-bound provider response fields/values, https://developers.openai.com/api/reference/resources/chat , https://platform.claude.com/docs/en/api/http/messages/create , https://aistudio.google.com/api-reference/generate-content . |
| `model_client.py:320,326` | empty string | safety-bound response text concatenation. |

### URL-load continuation: Step 0 and configuration impact

R-015/R-018/R-021: reuse the existing transport URL policy at profile load, preserving sentinel handling and the call-time check. Unknowns: none requiring a requirement change. Verify concrete backend `base_urls` and `endpoints.chat` independently, including when the other value is unresolved; report configuration gaps honestly. Both paths are configurable in `enhancer_config.v0`, example and inherited profiles retain their existing deployment sentinels. The profile-load tests use concrete HTTPS/loopback overrides and unsafe scheme, host and relative-route refusals. URL protocol and traversal checks are safety-bound. Finish with focused and full tests, mutations, and an explicit audit of remaining work.

### URL-load continuation report

**Status: IMPLEMENTING, incomplete.** `config.py` now calls the existing `model_transport.endpoint_url` during profile load for each concrete base URL and the chat route, separately when either other value is a sentinel. Call-time validation remains. `tests/test_model_config.py` exercises refused schemes, hosts and routes, including unresolved counterparts. `tests/test_field_contracts.py` and `tests/test_loader.py` use valid URL/relative-route overrides. Source-verified evidence: `model_transport.py:endpoint_url` owns the policy; `ModelTransport.post` calls it again. No external provider behavior is asserted.

| Operational value | Validation and profile | Override / refusal | Literal classification |
|---|---|---|---|
| `vlm.backends.*.base_urls` | `enhancer_config.v0`; example uses deployment sentinel; concrete URLs checked at load and call | `test_config_values_through_profile_load`, `test_config_override_and_refusal`, `test_unsafe_url_refused_at_profile_load` | configurable; URL policy safety-bound |
| `vlm.backends.*.endpoints.chat` | `enhancer_config.v0`; example uses deployment sentinel; concrete route checked at load and call | `test_overlay_engine_overrides`, `test_config_values_through_profile_load`, `test_unsafe_url_refused_at_profile_load` | configurable; route policy safety-bound |

The newly retained placeholder `chat` and `https://localhost` are safety-bound validation scaffolding used only to check the other independent configured value, never sent; diagnostic path fragments are artifact-bound, control booleans and branches are safety-bound. Test URL and route values are test-only. The older audit rows and exhaustive classification of all pre-existing T-009 runtime literals remain incomplete; previous profile-load-only tests do not prove their runtime effects. Consequently the task is not DONE.

**Unit/mock_contract checks:** using `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, project venv python, worktree `tools/agent-workflow/test_slot.py`, `-B -X faulthandler -m pytest -q -p no:cacheprovider`: `tests/test_model_config.py tests/test_model_transport.py --basetemp=.tmp/T-009/url-load-focus --tb=short` returned `118 passed in 14.46s`; `tests/test_field_contracts.py tests/test_loader.py --basetemp=.tmp/T-009/url-fixture-focus --tb=short` returned `565 passed, 24 skipped in 135.34s`. First full suite returned `4 failed, 1014 passed, 25 skipped in 162.94s`; the four failures were invalid override fixtures, fixed before the subsequent run.

**Mutants:** scratch source, same limiter and `tests/test_model_config.py -k unsafe_url_refused_at_profile_load --tb=line`: replacing configured base in the first check was killed (`4 failed, 4 passed, 103 deselected`); replacing its route survived (equivalent here because the independent second check validates the same concrete route); replacing the route in that independent check was killed (`1 failed, 7 passed, 103 deselected`). Ruff lint/format `not_run`: `python -B -m ruff --version` returned `No module named ruff`. Linux, E12, GPU, live cloud and engine behavior `not_run` by scope. `git diff --check` on the changed tracked code, tests and task file exited 0 before this report edit.

**Next:** finish each earlier configuration row with a real override effect and refusal or a justified owner, classify every literal in all changed and untracked runtime files including long literals, then independently review the patch. Do not repeat live model work or treat a double as engine evidence. The single-run ledger remains T-006-owned for durable cross-process accounting. No service was started.

**Final full suite (unit/mock_contract):** the same command prefix with `tests --basetemp=.tmp/T-009/url-load-full-final --tb=short` returned `1018 passed, 25 skipped in 169.31s (0:02:49)`. Scratch cleanup of this continuation's verified paths was rejected by command policy before execution; the mutation script was removed separately. Directories remaining under `.tmp/T-009/`: `url-mutants`, `url-mutant-test-1`, `url-mutant-test-2`, `url-mutant-test-3`, `url-mutant-test-repeat`, `url-mutant-3-repeat`, `url-load-focus`, `url-fixture-focus`, `url-load-full`, `url-load-full-final`; PM cleanup is needed.

### Configuration evidence reconciliation (2026-11-15; incomplete)

**Continuation completion report — status: BLOCKED for acceptance.** The requested exhaustive classification and profile-to-effect evidence were not completed in this continuation. The concrete acceptance blockers are the unclassified literals described below, the unproven effect paths listed row by row, and unassigned ownership/effect of `vision.quality` and `vision.transport`. No external permission or test failure caused those audit gaps; they remain work to do. No runtime or test source was changed in this continuation.

**Full suite (unit/mock_contract):** `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src <project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/audit-final --tb=short` returned `1018 passed, 25 skipped in 153.93s (0:02:33)`. No live engine, GPU, cloud, E12, Linux or ruff run was authorized/available; `not_run`. `git diff --check -- tasks/active/T-009-model-client.md` exited 0 before this final report edit. Cleanup of the verified `.tmp/T-009/audit-final` directory was rejected by command policy before execution; it remains for PM cleanup alongside the previously listed scratch.

These tests run in the full suite below. `test_backend_operational_schema`, `test_config_values_through_profile_load`, and `test_role_values_through_command_load` verify that a changed value survives loading and that invalid/missing values are refused; they do not by themselves prove profile-to-runtime effects. `test_model_backend_read_refuses_operational_sentinel` verifies refusal when that backend is read. A direct unit or labelled double test below demonstrates a runtime boundary but does not establish profile-to-boundary wiring unless explicitly stated.

| Older audit key | Override and refusal evidence; residual |
|---|---|
| `http.connect_timeout_s` | `test_backend_operational_schema`; `test_configured_timeouts_and_bounded_backoff` observes transport setting; profile-to-effect not established. |
| `http.read_timeout_s` | Same; `test_transport_timeout_and_cancellation_release_concurrency` uses synthetic timeout; profile-to-effect not established. |
| `http.write_timeout_s` | `test_backend_operational_schema`; `test_configured_timeouts_and_bounded_backoff`; profile-to-effect not established. |
| `http.pool_timeout_s` | `test_backend_operational_schema`; `test_configured_timeouts_and_bounded_backoff`; profile-to-effect not established. |
| `http.max_retries` | `test_backend_operational_schema`; `test_configured_timeouts_and_bounded_backoff`, `test_retry_reserves_budget_per_attempt`; profile-to-effect not established. |
| `http.backoff_initial_s`, `http.backoff_max_s` | `test_backend_operational_schema`, `test_nonfinite_and_backoff_order_refused`; `test_configured_timeouts_and_bounded_backoff`; profile-to-effect not established. |
| `http.retry_statuses` | `test_backend_operational_schema`; `test_retry_reserves_budget_per_attempt`; profile-to-effect not established. |
| `probe.max_requests` | `test_backend_operational_schema`; `test_probe_requires_evidence_and_distinguishes_incomplete_and_failed`; profile-to-effect not established. |
| `probe.timeout_s` | `test_backend_operational_schema`; `test_probe_timeout_is_failure`; profile-to-effect not established. |
| `image.max_decode_pixels` | `test_backend_operational_schema`; `test_decode_limit_and_bad_limits`; profile-to-effect not established. |
| `anthropic_version` | `test_anthropic_version_override_and_refusal`; `test_cloud_requires_ledger_and_uses_synthetic_key` observes synthetic header; profile-to-header not established. |
| `vlm.default_backend`, role `backend` | `test_binding_rules_through_profile_load`, `test_role_values_through_command_load`; `test_cloud_budget_exhaustion_reprobes_and_falls_back` and `test_cloud_image_forbidden_falls_back_and_reprobes_local`; profile-to-fallback not established. |
| `provider` | `test_config_override_and_refusal` and `test_backend_enum_refused`; cloud/local tests use direct settings; profile-to-effect not established. |
| `base_urls`, `endpoints.chat` | `test_config_values_through_profile_load`, `test_unsafe_url_refused_at_profile_load`, `test_overlay_engine_overrides`; `test_retry_reserves_budget_per_attempt` observes joined route; profile-to-request not established. |
| `endpoints.health` | Consumer: T-016 per PM decision pending user review; no T-009 runtime effect applies. |
| `api_key_file` | `test_config_values_through_profile_load`; `test_synthetic_credential_and_path_refusal` and `test_cloud_headers_use_synthetic_credentials` use synthetic keys; profile-to-header not established. |
| `model.id`, `model.revision` | `test_binding_rules_through_profile_load`, `test_config_values_through_profile_load`; `test_role_client_records_effective_body_and_validates_json` exercises record/body without asserting these overrides; effect evidence incomplete. |
| `model.source`, `model.file`, `model.mmproj_file` | Consumer: T-016 per PM decision pending user review; no T-009 runtime effect applies. |
| `max_concurrency` | `test_config_values_through_profile_load`, `test_zero_concurrency_refused`; `test_concurrency_limit_and_cancellation`; profile-to-resource construction not established. |
| `call_budget` | `test_config_values_through_profile_load`, `test_invalid_limits_refused`; `test_budget_concurrent_reservations_and_single_use`; profile-to-ledger construction not established. |
| `allow_images`, `on_budget_exhausted` | `test_config_values_through_profile_load`; fallback doubles named above; profile-to-effect not established. |
| `unsupported_param_policy` | Only `warn` is permitted: `test_backend_policy_refused` and `test_undocumented_fields_warn_and_none_is_omitted`; non-default override is inapplicable to a single-value enum. |
| `extra_body` | `test_config_values_through_profile_load`; `test_role_client_records_effective_body_and_validates_json` observes merge-last; profile-to-effect not established. |
| role `mode` | Single allowed value `generate`: `test_role_mode_refused`; non-default override inapplicable. |
| role `prompt_id`, `max_tokens` | `test_role_values_through_command_load`; `test_role_client_records_effective_body_and_validates_json`, `test_gemini_documented_fields` use direct settings; profile-to-record/request effect not established. |
| role sampling fields | Each has override/refusal in `test_role_values_through_command_load` and nonfinite refusal where numeric in `test_nonfinite_sampling_refused`; `test_gemini_documented_fields`, `test_local_engine_fields_require_probe`, `test_undocumented_fields_warn_and_none_is_omitted` cover only a subset of runtime mappings; remaining per-field effects unproven. |
| reasoning `mode`, `effort`, `budget_tokens`, `template_kwargs_key` | `test_role_values_through_command_load`, `test_binding_rules_through_profile_load`; `test_documented_cloud_reasoning_fragments_and_unsupported_switch`, `test_thinking_probe_requires_observable_switch_effect`; profile-to-effect and real engine evidence unproven. |
| image `source`, image/vision `transport`, output `transport` | Consumers T-010/T-014 for image source/transport and T-015 for output transport, per PM decision pending user review; vision transport ownership specifically remains unassigned and open. |
| image `max_side`, vision `max_image_side` | `test_role_values_through_command_load`, `test_config_values_through_profile_load`; `test_image_requires_verified_capability_and_is_resized`, `test_resize_dimensions`; profile-to-effect not established. |
| image/vision `format`, `quality` | `test_role_values_through_command_load`, `test_config_values_through_profile_load`, `test_image_members_required_and_max_side_can_inherit`; effect through actual loaded profile not established; vision quality usage needs owner/effect resolution. |
| output `format`, `schema_id`, `strict`, `validate_response` | `test_role_values_through_command_load`, `test_binding_rules_through_profile_load`, `test_writer_resolution`; `test_role_client_records_effective_body_and_validates_json`, `test_all_project_schemas_load`; per-field profile-to-effect not established; `validate_response` is not independently read by the client. |
| storage, device, persistence, lifecycle | Storage and durable cross-process ledger consumer T-006; deployment/device consumer T-016; client closes its supplied transport (`test_transport_timeout_and_cancellation_release_concurrency`); integrations not proven by T-009. |

No row above asserts evidence that its named test does not provide. Configuration audit remains open until each applicable runtime effect is demonstrated and the unresolved ownership is decided.

### Runtime literal audit (partial; not acceptance)

The existing `.tmp/T-009/runtime-literals-inventory.txt` lists 595 lines of unique short literals including headings, but excludes long and multiline literals and does not cover every occurrence. It is an inventory lead, not a completed audit. The following individually checked examples do not claim to classify every remaining literal. Provider URLs below are citations from the prior provider scout, not pages independently re-read during this continuation; the scout's extraction limitations remain.

| File:line | Literal | Class and evidence |
|---|---|---|
| `model_client.py:129,216` | `BUDGET_EXHAUSTED` | artifact-bound call-record warning; `contracts/vlm-request.md`. |
| `model_client.py:171,176,178` | `PARAM_UNSUPPORTED` | artifact-bound call-record warning; `contracts/vlm-request.md`. |
| `model_client.py:265,266` | `SCHEMA_INVALID` | artifact-bound call-record status; `contracts/vlm-request.md`. |
| `model_client.py:185,188` | `response_format`, `json_object`, `json_schema` | protocol-bound; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create (prior scout extraction). |
| `model_cloud.py:17` | `Authorization`, `Bearer `, `anthropic-version` | protocol-bound; API overview quotations under Review fix round 1. |
| `model_cloud.py:20` | `x-goog-api-key` | protocol-bound; Gemini REST example under Review fix round 1. |
| `model_cloud.py:29` | `1024` | protocol-bound under the PM decision; https://platform.claude.com/docs/en/api/http/messages/create (prior scout extraction). |
| `model_schemas.py:15,20` | `*.schema.json`, `.schema.json` | artifact-bound local schema naming convention. |
| `model_transport.py:39,40` | `follow_redirects=False`, `trust_env=False` | safety-bound transport restrictions. |
| `model_transport.py:14-24` | `http`, `https`, `localhost`, traversal and URL separators | safety-bound URL policy, with the HTTP scheme vocabulary protocol-bound. |
| `config.py:460-475` | `chat`, `https://localhost` | safety-bound placeholders used solely to check a concrete counterpart while a deployment sentinel remains unresolved; never sent. |

Open classification: the remainder of `config.py` and all nine `model_*.py` files, including long/multiline and repeated occurrences. In particular EXIF tag `274` and Pillow's independent decompression-bomb limit need primary-source and ownership review. These omissions are open audit defects; they cannot be labelled `configurable`, `protocol-bound`, `artifact-bound`, `safety-bound`, `test-only`, or `defect` by guessing.

### Provider-binding continuation (incomplete; supersedes older status)

**Current status: IMPLEMENTING.** The current suite is green, but the exhaustive literal classification and the earlier per-key configuration effect/refusal audit are incomplete, so this is not `DONE`. The remaining URL safety rules in `endpoint_url` are still enforced at call time, not fully at profile load. The next worker must complete the per-literal inventory, finish the unproven audit rows and move or validate those URL rules at load, with tests; verify the proposed stage ownership with the PM. This continuation stops at a clean handoff near the context limit.

**Work completed:** `config.py` checks default/backend binding, cloud capability refusals, Anthropic thinking budget, unknown `provider_default`, finite sampling, output/schema combinations and installed Pillow encoders at profile load. `model_client.py` requires explicit role image format/quality and retains only the approved `max_side` inheritance. The two config schemas enforce required image members and the numeric/string constraints; example cloud URLs and unmeasured role image members use existing sentinels. Test fixtures and table-driven profile tests cover the changes. Gemini image roles with `allow_images: false` retain the required local fallback; true fails closed.

**Research evidence:** installed Pillow `PIL/Image.py:383` says `init()` “loads all available file format drivers”; `Image.SAVE` is the installed save registry. Anthropic minimum is a PM decision pending user review and previously noted by `.tmp/T-009/provider-docs.md`; no provider base URL was established from a primary source and `provider_default` is refused. No live engine/cloud evidence is claimed.

| Configuration / behavior | Validation / example | Override and refusal evidence | Class |
|---|---|---|---|
| `vlm.default_backend`, role `backend`, backend `provider` | Existing local default and existing bound backend required; example `main` / `openai_compatible` | `test_binding_rules_through_profile_load` valid local / refuses missing or cloud default and unknown role backend; unresolved selected/default backends refused by loader read-set | configurable |
| backend `base_urls` | Explicit cloud URL required; `deploy_required` in examples | same table accepts `https://example.org`, refuses `provider_default`; current primary documentation is insufficient for auto-resolution | configurable; refusal safety-bound |
| model `id`, `revision` | Nonempty strings or unresolved sentinels; examples `deploy_required` / `pin_required` | same table accepts concrete strings and rejects empty strings | configurable |
| role `reasoning.mode`, `budget_tokens`, `max_tokens` | Anthropic `on` requires 1024 <= budget < max tokens; local/Gemini do not inherit that limit | same table accepts 1024 and rejects 1023/equality; `test_anthropic_limit_does_not_apply_to_other_providers` covers local and Gemini | configurable; 1024 protocol-bound under PM decision |
| role `output.format`, `schema_id` and provider | Cloud structured output fails closed; local JSON schema requires a concrete ID | same table checks valid `none`, refused cloud JSON and missing concrete schema; manifest-dependent cloud output also fails closed | configurable; refusal safety-bound |
| role `image`, cloud `allow_images` | Gemini images fail closed when cloud images are enabled; when disabled, local fallback remains | same table checks text-only and both `allow_images` choices; labelled in-memory fallback double in `test_cloud_image_forbidden_falls_back_and_reprobes_local` | configurable; refusal safety-bound |
| role `image.source`, `format`, `quality`, `transport`; `max_side`; `vision.format`, `vision.quality` | Members explicit except max-side inheritance; format must be a Pillow save encoder; vision quality 1–100 | `test_image_members_required_and_max_side_can_inherit`, table encoder and quality cases, `test_image_requires_verified_capability_and_is_resized` with and without inheritance | configurable; encoder registry structural |
| role sampling `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `stop` | Schema types/ranges and no nonfinite floats | `test_nonfinite_sampling_refused`, `test_role_values_through_command_load`; downstream provider effect coverage remains incomplete as in earlier audit | configurable |

**Literal inventory, partial:** `.tmp/T-009/runtime-literals-inventory.txt` lists enumerated short literals from `config.py` and all nine `model_*.py` files by file and first line; it excludes long/multiline literals and does not classify them individually. Wire field/header/status names and EXIF tag 274 are protocol-bound; schema filenames and record keys are artifact-bound; configured field keys/values are configurable; control bounds, failure diagnostics, local path restrictions and booleans are safety-bound; fixture values are test-only. The list still needs an item-by-item audit, including all omitted literals, before acceptance. Pillow's own decompression-bomb warning threshold is a separate library safety limit in addition to the configured decode bound and needs classification/review. No exhaustive classification or completed configuration audit is claimed.

**Mutation evidence:** using a scratch copy and the worktree limiter against `tests/test_model_config.py -k binding_rules`, removing all binding checks produced `11 failed, 4 passed, 87 deselected`; changing Anthropic minimum to zero produced `1 failed, 1 passed, 100 deselected`; bypassing encoder validation produced `1 failed, 101 deselected`. All three mutants were killed. Earlier full run before fixing error precedence returned `1 failed, 1007 passed, 25 skipped`; the failed `test_retrieval_block_sentinel_is_reported` passed in the subsequent focused run (`241 passed in 24.62s`). The resize effect double returned `9 passed in 1.76s`.

A fourth mutant ignored `allow_images` when checking Gemini images; `test_binding_rules_through_profile_load` killed it (`1 failed, 1 passed, 101 deselected`). The prior full run on the immediately preceding test version returned `1009 passed, 25 skipped in 163.84s`; a later image-inheritance test changed after that collection, so the current suite result is recorded separately below.

**Final full suite (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/load-policy-current --tb=short` returned `1010 passed, 25 skipped in 163.86s (0:02:43)`. `git diff --check` over the changed config, schema, runtime, test and task files exited 0. Ruff lint and format are `not_run` (not installed); Linux, E12, GPU and live cloud calls are `not_run` by scope. No model server or GPU job was started. Scratch remains for PM cleanup, including `.tmp/T-009/runtime_literals.py`, `runtime-literals-inventory.txt`, `load-policy-*`, `binding-mutant-*`, and the prior scratch trees.

### PM schema-gap continuation: configuration class sweep

**Current status: IMPLEMENTING, incomplete; supersedes the older blocker/status lines below.** The PM-approved schema fixes and the tests below pass, but the sweep exposes remaining schema/consumer mismatches and ignored values, so T-009 is not ready for acceptance. Closing provider-dependent cross-field constraints and the rest of the configuration audit remains work for the PM to assign/resolve; no unsupported mapping was invented.

**Full check (unit/mock_contract):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/schema-sweep-final --tb=short` returned `957 passed, 25 skipped in 175.99s (0:02:55)`. The previous collected version failed two field-contract fixture checks (`2 failed, 950 passed, 29 skipped`); fixtures were corrected before this run. `ruff` lint and format, Linux, E12, GPU and live cloud `not_run` (ruff absent; other work outside dispatch). No service or model was started. Leave `.tmp/T-009/continuation-*`, `mutant-*`, `schema-*` and the two sweep scripts for PM cleanup as instructed.

Source-verified comparison of `enhancer_config.v0`, `vlm_role.v0`, `config.py`, and `model_client`, `model_cloud`, `model_mapping`, `model_transport`, `model_resources`, `model_image`, `model_probe`. A profile-load override does not by itself prove an effect. Sentinel values remain template values, refused when read. Results below are not a claim that the full configuration audit has passed.

| Keys checked | Result and residual gap |
|---|---|
| `vlm.default_backend`, role `backend` | Role backend existence is checked in `load_config`; default backend existence and local-provider constraint remain unverified at load. Fallback consumes it dynamically; JSON Schema alone cannot generally compare arbitrary map keys to another property's value. |
| backend `provider`, `unsupported_param_policy` | Provider has a three-value enum and policy only `warn`; optional unsupported fields warn/drop. Consumer does not read policy separately. |
| backend `base_urls`, `endpoints.chat`, `endpoints.health` | Empty list, empty element, missing endpoints and empty chat fixed in schema. Full URL and relative-route safety are still checked only by `endpoint_url` at call time; `provider_default` is accepted in cloud examples but is not a usable URL. `health` is schema-required but not read by this client. |
| backend `api_key_file`, `anthropic_version` | Cloud key lexical shape narrowed to one file in `.secrets/`; version is a date-shaped string or deployment sentinel. Actual key read and path resolution remain runtime checks; no credential was read. |
| backend `model.id`, `model.revision`, `model.source`, `model.file`, `model.mmproj_file` | ID and revision are strings used in body/record; empty strings remain schema-accepted and downstream model/engine acceptance is unverified. Source and artifact files are server-owned, not read by this client. |
| backend `max_concurrency`, `call_budget`, `on_budget_exhausted`, `allow_images`, `extra_body` | Positive concurrency fixed in schema; budget is nonnegative and cloud policy/image fields required. `extra_body` remains an open object and is merged last. Caller supplies ledger and concurrency objects; profile-to-resource construction remains unverified. |
| backend `http.connect_timeout_s`, `read_timeout_s`, `write_timeout_s`, `pool_timeout_s`, `max_retries`, `backoff_initial_s`, `backoff_max_s`, `retry_statuses` | Schema has positive timeouts, nonnegative retry/backoff and bounded status integers; loader checks finite values and maximum >= initial. Focused transport double tests behavior; no real timeout expiry proof. |
| backend `probe.max_requests`, `probe.timeout_s`, `image.max_decode_pixels` | Schema positive bounds agree with probe and resize checks; loader also checks finite probe timeout. Profile-to-effect coverage remains incomplete. |
| role `mode`, `prompt_id`, `max_tokens` | Mode is fixed to `generate`; prompt id is nonempty; max tokens positive. Prompt id enters record; actual prompt is caller supplied. |
| role sampling `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `stop` | Concrete role schema checks types and numeric ranges. Provider-specific support is probed or warned/dropped; provider-specific ranges and nonfinite sampling are not fully closed by config validation. |
| role reasoning `mode`, `effort`, `budget_tokens`, `template_kwargs_key` | Concrete role schema checks enums/types; Anthropic requires budget >= 1024 and < max tokens only in `cloud_request`, while schema cannot relate arbitrary role-backend bindings to provider with ordinary fixed JSON Schema. This gap remains open; do not enforce Anthropic limits globally on other providers. |
| role image `source`, `max_side`, `format`, `quality`, `transport`; vision `max_image_side`, `format`, `quality`, `transport` | Positive size bounds match resize. Image format can name an unsupported encoder, and several image members may be absent and fall back implicitly; vision quality permits values outside the role quality range. `source` and transport are not read by this client. These remain open, not proven equivalent or fixed. |
| role output `format`, `schema_id`, `strict`, `transport`, `validate_response` | Enums/types checked, schema IDs resolved by loader, structured output requires validation. Cloud structured output and Gemini image mappings precisely refuse as in the prior report. Client does not use output transport; writer manifest and provider-dependent combinations need further cross-field validation. |

**Mutation survivor explained:** `.tmp/T-009/compare_role_schemas.py` compared the full concrete role schema with its nested enhancer-config copy after recursively removing sentinel-only `anyOf` branches and `$id`/`$schema`; output `equivalent after removing sentinel branches: True`. `load_config` validates the whole config first and `_sentinel` recursively refuses sentinel values in the read role before the separate concrete-role check. Thus bypassing that second validation is equivalent for the current two schemas and read path; this is not a reason to remove the defense against later schema drift.

**Changed schema literals:** concurrency minimum 1 and base URL count minimum 1 are `safety-bound`; empty URL/route minimum length 1 and the cloud credential lexical path pattern are `safety-bound`; `deploy_required` is an existing `configurable` sentinel. Examples, URLs and key names in tests are `test-only`. The exhaustive inventory of all earlier T-009 runtime literals remains incomplete as previously reported.

| Changed configuration | Validation / example | Override and refusal evidence | Class |
|---|---|---|---|
| `vlm.backends.*.max_concurrency` | Integer >= 1 or sentinel; example `fit_from_bench` | `test_config_values_through_profile_load` uses 3 / refuses 0; focused suite 186 passed | configurable |
| `vlm.backends.*.base_urls` | Nonempty list of nonempty strings or sentinel; example `[deploy_required]` locally | `test_config_values_through_profile_load` uses loopback URL / refuses `[]` and `['']`; complete URL safety remains a runtime check | configurable |
| `vlm.backends.*.endpoints.chat` | Required endpoints object or sentinel, nonempty chat; example `deploy_required` | `test_config_values_through_profile_load` uses `chat` / refuses empty or missing member; `test_endpoint_missing_is_refused` covers missing object; unresolved sentinel path is not separately tested | configurable |
| `vlm.backends.*.api_key_file` (cloud) | One `.secrets/<filename>` component or sentinel; example `.secrets/anthropic.key` | `test_config_values_through_profile_load` uses synthetic path / refuses `none` and a nested path, without opening either | configurable |

The focused command used `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src` and `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests/test_model_config.py tests/test_loader.py --basetemp=.tmp/T-009/schema-sweep-focus-2 --tb=short`: `186 passed in 21.25s` before the later missing-endpoint test. The same limiter and interpreter, with scratch `PYTHONPATH=.tmp/T-009/continuation-mutation/src`, ran `pytest -q -p no:cacheprovider .tmp/T-009/continuation-mutation/tests/test_model_config.py` for each of `-k max_concurrency`, `-k base_urls`, `-k api_key_file`, each with its own `--basetemp=.tmp/T-009/schema-mutant-{1,2,3} --tb=line`. Reverting the positive concurrency bound gave `1 failed, 55 deselected`; removing the nonempty URL-list bound gave `1 failed, 1 passed, 54 deselected`; removing the cloud credential path constraint gave `2 failed, 54 deselected`. All three mutants were killed. Scratch trees are left for PM cleanup per dispatch.

### Continuation after limiter correction (2026-11-15; incomplete)

- **Status: BLOCKED, incomplete.** The PM corrected the limiter location to the worktree copy. The previous main-tree lock permission blocker no longer applies. The same zero-concurrency refusal failed in both the focused and full suite; footer rule 17 requires stopping after the same step fails twice. No runtime code was changed in this continuation.
- **Work completed:** `tests/test_model_config.py` has table-driven profile-load checks for 26 role fields and 12 additional configuration fields. The original role table returned `39 passed in 7.56s`; after adding the extra table and strengthening four overrides, the focused run returned `1 failed, 50 passed in 8.59s`. The failure is the intentional assertion that `vlm.backends.main.max_concurrency: 0` must be refused at load: the existing schema at `contracts/schemas/enhancer_config.v0.schema.json:3317` allows zero, while `BackendConcurrency` requires a positive integer (`src/prompt_enhancer/model_resources.py`). The existing-key schema change is outside the brief's schema scope for new operational keys. The test remains failing; do not weaken it to obtain a green result.
- **Results:** With `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, the current-state full command was `<project>/.venv/Scripts/python.exe -B tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -B -X faulthandler -m pytest -q -p no:cacheprovider tests --basetemp=.tmp/T-009/continuation-full-current --tb=line`. Output: `FAILED tests/test_model_config.py::test_config_values_through_profile_load[vlm.backends.main.max_concurrency-3-0]`, `1 failed, 947 passed, 25 skipped in 151.25s (0:02:31)`. Earlier full run, collected before the extra table: `936 passed, 25 skipped in 157.01s (0:02:37)`; it is not the final test state. Focused run used the same command prefix with `tests/test_model_config.py --basetemp=.tmp/T-009/continuation-config-2 --tb=short`. `git diff --check -- tests/test_model_config.py tasks/active/T-009-model-client.md` exited 0 before this report update. `ruff` lint and format `not_run`: main venv reports `No module named ruff`.
- **Mutations (scratch copies, unit):** bound-backend lookup omitted: killed by the role backend refusal (`1 failed, 38 deselected`); local schema lookup omitted: killed by the schema-id refusal (`1 failed, 38 deselected`); concrete role-schema validation omitted: survived (`26 passed, 13 deselected`). These sample loader checks rather than new runtime lines, since no runtime line was changed in this continuation; they do not establish coverage of the preceding worker's latest runtime changes. Survivor remains open for review; the whole-config schema may also reject the tested invalid values, so this result alone does not establish a runtime defect.
- **Cloud mapping (source-verified current refusal):** `model_cloud.py` refuses all non-`none` cloud output with `cloud structured output mapping is not yet verified` and Gemini image construction with `Gemini image field members are not verified`; `RoleClient` can refuse Gemini images earlier with `gemini: image request mapping unproven`. Other undocumented optional sampling/reasoning fields warn and drop as recorded in `model_mapping.py`. The Gemini header is documented on a different endpoint in `.tmp/T-009/provider-auth.md`; its generateContent behavior remains unverified. No primary page was re-read, no new mapping was added and no live support is claimed.
- **Literal classification, partial:** provider wire keys, HTTP methods/status/response vocabulary and Anthropic's documented 1024 thinking minimum are `protocol-bound` subject to the existing provider-note evidence caveats; the local schema filename/id convention is `artifact-bound`; URL/path traversal checks, positive semaphore limits, distinct challenge requirements, refusal statuses and decode guard are `safety-bound`; sentinels and configured timeouts/retries/probe/image/budget values are `configurable`; synthetic fixtures and mutation inputs are `test-only`. `model_client.py` uses `from_vision` and vision quality as implicit defaults for omitted optional role image members, and ignores configured image/output transports; classify these as **defects pending resolution**, not accepted defaults. The exhaustive per-literal inventory and per-field effect audit are incomplete; no acceptance is claimed.
- **Configuration audit update:** the previous incomplete tables remain open. The table tests loader acceptance/refusal, not a runtime effect; `output.format`, `schema_id`, image/vision settings, retry/probe settings and all other rows still need independent profile-to-effect evidence where applicable. The zero concurrency case above is a concrete load-path defect. The configured `output.transport`, role/vision image `transport`, and role image `source` have no observed client read; `validate_response` is not read by the client, which validates according to output format. Ownership and behavior require resolution before marking the rows complete.
- **Not run:** ruff, Linux, E12, real engine thinking/image probes and live cloud calls. No model server, GPU or cloud call was started. The durable cross-process ledger remains T-006-owned.
- **Cleanup:** The attempt to remove this continuation's verified scratch directories (`continuation-mutation`, `mutant-1` through `mutant-3`, `continuation-config`, `continuation-config-2`, `continuation-full` under `.tmp/T-009/`) was rejected by the command policy before execution. They remain for PM cleanup, as does `continuation-full-current`.

### Continuation report (2026-11-15; incomplete)

- **Status: BLOCKED on test-slot permissions.** The PM-approved main-tree limiter resolves its lock directory from its own location and cannot create `<project>\.tmp\pm-state\test-slots\slot-0.lock` under this worker's workspace-only write permissions. No pytest process started. A separate worktree lock would not enforce the machine-wide limit, so it was not substituted.
- **Work completed:** Added `test_role_values_through_command_load` and its table in `tests/test_model_config.py` for 26 role fields, using `profile register` and invalid values. This test is unverified and is not a complete operational audit: loading a value alone does not prove an effect, and the `backend` override equals the fixture baseline. No runtime code was edited. The earlier PM approval was read from the supplied main-tree path.
- **Evidence:** `<project>/.venv/Scripts/python.exe -B -I tools/agent-workflow/recall.py build` returned `indexed=0 unchanged=84 removed=0 refused=0 chunks=0 skipped_chunks=0`; search `T-009 profile override refusal provider audit` returned no output. `git diff --check -- tests/test_model_config.py tasks/active/T-009-model-client.md` exited 0 before this report edit.
- **Failed command (no tests executed):** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=src`, `<project>/.venv/Scripts/python.exe -B <project>/tools/agent-workflow/test_slot.py -- <project>/.venv/Scripts/python.exe -X faulthandler -m pytest -q tests/test_model_config.py --basetemp=.tmp/T-009/continuation-config` exited 1: `PermissionError: [Errno 13] Permission denied: '<project>\\.tmp\\pm-state\\test-slots\\slot-0.lock'`.
- **Static findings (source-verified):** `model_client.py` constructs OpenAI-compatible image data URIs and `response_format` without reading the configured image/output `transport`; image `source` is not consulted there, and response validation follows output format rather than reading `validate_response`. Their ownership and runtime effects need review and tests before an audit can pass. `model_cloud.py` refuses non-`none` cloud output with `cloud structured output mapping is not yet verified`, and Gemini images with `Gemini image field members are not verified`; `model_client.py` can refuse Gemini images earlier with `gemini: image request mapping unproven`. No new cloud mapping or live-support claim was made. Gemini authentication remains documented on a different endpoint as described in `.tmp/T-009/provider-auth.md`.
- **Configuration audit:** Existing incomplete tables below remain in force; the new role table is `not_run` and does not close any row. A full literal inventory, per-key effect/refusal evidence and any fixes remain outstanding. Configuration impact remains the previously approved table; no new runtime change or key was introduced in this continuation.
- **Not run:** focused and full pipeline tests, all continuation mutants, lint and `ruff format --check` (the venv reports `No module named ruff`), Linux, E12, GPU, real engine probes and cloud calls. No server or model process was started. No new scratch tree was created by the failed test command.
- **Next:** arrange a way for the approved machine-wide limiter to create its lock within the worker's permitted filesystem; rerun the new table and fix its failures, complete all operational and role effect/refusal cases and literal classifications, then run mutations and the full suite. Do not treat prior worker results or this unrun table as current verification. Durable cross-process budget accounting remains T-006-owned.

### Current status

- **IMPLEMENTING, incomplete:** role lookup, writer resolution, caller-supplied probe cases through the transport, image preparation, local schema checks and a body hash are connected. Image and thinking probes now use pairs of requests differing only in image or configured switch, and test doubles reject ignored changes; the thinking observer and probe fixtures remain caller-supplied and require documented engine-specific evidence. Local fallback after image restriction or exhausted cloud budget re-probes; truncation and refusal tests use a labelled double. PM confirmed `contracts/vlm-request.md:61` requires the request hash, not the full body: the record stores hashes, not image bytes or credentials. Anthropic Bearer/version and Gemini API-key headers use runtime credentials; Gemini's header is documented for a different endpoint and needs later live verification. Cloud text handling and Anthropic image construction are partial; unverified structured output and Gemini image fields fail closed. Complete mapping coverage, tests and audit remain incomplete. Do not deploy or consider the done check passed.
- **Checks (unit/mock_contract):** `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src <venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -m pytest -q tests --basetemp=.tmp/T-009/pytest-full-final-state` returned `910 passed, 25 skipped in 160.44s`. Focused run through the same limiter on `tests/test_model_probe.py tests/test_model_client.py tests/test_model_cloud.py tests/test_model_mapping.py tests/test_model_secrets.py tests/test_model_schemas.py tests/test_model_resources.py tests/test_model_transport.py tests/test_loader.py::test_model_backend_read_refuses_operational_sentinel tests/test_loader.py::test_overlay_engine_overrides --basetemp=.tmp/T-009/pytest-all-targeted` returned `53 passed in 4.63s`; subsequent `tests/test_model_cloud.py tests/test_model_client.py tests/test_model_probe.py --basetemp=.tmp/T-009/pytest-last-cloud` returned `20 passed in 1.64s`. `git diff --check` over the selected tracked T-009 files exited 0; it does not cover untracked files.
- **Mutations:** at least three selected scratch mutants in each changed Python module (`config`, `model_client`, `model_cloud`, `model_image`, `model_mapping`, `model_probe`, `model_resources`, `model_schemas`, `model_secrets`, `model_transport`) were killed by focused tests; three additional Anthropic thinking-budget boundary mutants were killed by `tests/test_model_cloud.py`. A zero-concurrency mutant initially survived; `test_zero_concurrency_refused` was added and killed it on retest. The first cloud mutation setup lacked a module; the PM authorized a complete scratch copy and three retries were killed. A mapping mutation cleanup and a separate attempted mutation were interrupted by a host-guard state error; neither is counted as a successful check. Earlier client mutants predate the newest client changes and are not comprehensive mutation coverage.
- **Open issues:** `.tmp/T-009/provider-auth.md` documents Anthropic auth and Gemini header on another endpoint; live Gemini generateContent header verification remains outstanding. vLLM schema member, Anthropic sampling, Gemini schema extraction and complete cloud image/output mappings remain unverified; cloud structured output and Gemini images fail closed. Anthropic's documented 1024 minimum thinking budget is a protocol-bound constant checked against configured `max_tokens`. Role/client tests cover only a fraction of required paths. Request hash still lacks engine/version and other required metadata. Probe fixtures and the thinking observer must be supplied by the caller; the documented note does not establish an engine response field proving thinking on/off, so the double is not evidence of live support. E12 `not_run`, GPU in use. Scratch copies and pytest folders remain under `.tmp/T-009/` for PM cleanup; lint and `ruff format --check` are `not_run` (ruff absent).

### Configuration audit (incomplete; no acceptance)

Each row uses `test_backend_operational_schema` for schema override/missing/invalid checks. Actual transport/probe/resize behavior is only partially tested, so none of these rows is complete. All are `configurable`; examples use the sentinels listed in Step 0.

| Configuration key | Validation and example | Override / refusal test and remaining work |
|---|---|---|
| `http.connect_timeout_s` | finite > 0; `deploy_required` | `test_backend_operational_schema`, `test_model_backend_read_refuses_operational_sentinel`, `test_configured_timeouts_and_bounded_backoff`; real connect expiry `not_run` |
| `http.read_timeout_s` | finite > 0; `deploy_required` | same schema, sentinel and timeout-setting tests; synthetic ReadTimeout and cancellation in `test_transport_timeout_and_cancellation_release_concurrency`; real read expiry `not_run` |
| `http.write_timeout_s` | finite > 0; `deploy_required` | same schema, sentinel and timeout-setting tests; real write expiry `not_run` |
| `http.pool_timeout_s` | finite > 0; `deploy_required` | same schema, sentinel and timeout-setting tests; real pool expiry `not_run` |
| `http.max_retries` | integer >= 0; `design_required` | schema, read-sentinel and `test_configured_timeouts_and_bounded_backoff`; full call-path refusal `not_run` |
| `http.backoff_initial_s` | finite >= 0; `design_required` | schema, read-sentinel, order and `test_configured_timeouts_and_bounded_backoff`; full call-path refusal `not_run` |
| `http.backoff_max_s` | finite >= initial; `design_required` | schema, read-sentinel, order and `test_configured_timeouts_and_bounded_backoff`; full call-path refusal `not_run` |
| `http.retry_statuses` | unique HTTP status integers; `design_required` | schema, read-sentinel and retry double; full call-path refusal `not_run` |
| `probe.max_requests` | integer > 0; `design_required` | schema, read-sentinel and `test_probe_requires_evidence_and_distinguishes_incomplete_and_failed`; profile-to-runtime override `not_run` |
| `probe.timeout_s` | finite > 0; `design_required` | schema, read-sentinel and `test_probe_timeout_is_failure`; profile-to-runtime override `not_run` |
| `image.max_decode_pixels` | integer > 0; `deploy_required` | schema, read-sentinel and `test_decode_limit_and_bad_limits`; profile-to-runtime override `not_run` |
| `anthropic_version` | date-shaped string; `deploy_required` | `test_anthropic_version_override_and_refusal` and `test_cloud_requires_ledger_and_uses_synthetic_key`; unresolved sentinel runtime refusal covered by `test_cloud_headers_use_synthetic_credentials`; configurable |

Existing role, provider, model, endpoint, sampling, reasoning, image, schema, concurrency, budget and lifecycle values still require the approved row-by-row operational audit. New runtime literals have not all been classified. Lint, `ruff format --check`, Linux and E12 remain `not_run`.

Additional existing operational values, all configurable and sourced from `configs/enhancer.example.yaml` and role/config schemas; runtime override/refusal coverage is **incomplete** for every row below, even when an existing focused test is named:

| Configuration value | Current evidence and missing audit |
|---|---|
| `vlm.default_backend` and role `backend` | image restriction and budget fallback doubles; full fallback and refusal `not_run` |
| backend `provider`, `base_urls`, `endpoints`, `api_key_file` | synthetic header and URL refusal tests; per-provider override/refusal `not_run` |
| backend `model.id`, `model.revision`, `max_concurrency` | model ID used in requests; concurrency double; per-key profile override/refusal `not_run` |
| backend `call_budget`, `allow_images`, `on_budget_exhausted` | concurrent ledger and fallback doubles; profile override/refusal per key `not_run` |
| backend `unsupported_param_policy`, `extra_body` | warn/drop and merge double; override/refusal per key `not_run` |
| role `mode`, `prompt_id`, `max_tokens` | partial mapping tests; per-key runtime override/refusal `not_run` |
| sampling `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `stop` | partial documented mapping tests; per-field runtime override/refusal `not_run` |
| reasoning `mode`, `effort`, `budget_tokens`, `template_kwargs_key` | synthetic thinking pair; per-field runtime override/refusal and engine-specific evidence `not_run` |
| role image `source`, `max_side`, `format`, `quality`, `transport`; vision `max_image_side`, `format`, `quality`, `transport` | property resize, decode and EXIF tests; per-field runtime override/refusal and K1 integration `not_run` |
| output `format`, `schema_id`, `strict`, `transport`, `validate_response` | local schema and invalid JSON double; per-field override/refusal and cloud structured output `not_run` |
| storage paths, device, lifecycle and persistence | caller-owned or not used by this patch; boundary verification and durable ledger integration `not_run` |


## Noticed, not done

- A total request deadline remains out of scope; configured HTTP timeouts are per phase.

- Durable cross-process budget accounting remains owned by T-006; the included ledger is single-run only.
- Query and verify the server engine version during the later E12 live run; this patch records the configured version only.

## Worker completion

- [x] Changes: partial role client and configuration, image, transport, probe, mapping, resource and schema components; see continuation report.
- [x] Checks: 910 passed and 25 skipped in the final suite; 53 focused tests passed. Selected mutants of every changed Python module were killed; one survivor was killed after adding a test, and earlier client mutants predate latest changes.
- [x] Cleanup: no model or server process started; scratch pytest folders remain under `.tmp/T-009/` after earlier cleanup was blocked by policy, and the first three belong to the PM.
- [x] Remaining: complete model client and provider/probe behavior, tests, remaining mutations and configuration audit; E12 is separate.


### Per-occurrence runtime literal inventory

AST inventory of numeric and non-diagnostic string constants in all changed/new runtime modules; column offsets distinguish repeated occurrences. Docstrings and exception diagnostic text are grouped as documentation and safety-bound diagnostics respectively. f-string fragments and empty strings are included. Categories are reconciled to the configuration, provider and contract groups above; provider-source limitations noted in the prior research still apply.

| File:line:column | Literal | Class / evidence |
|---|---|---|
| `config.py:15:23` | `'fit_from_gt'` | configurable: unresolved-value sentinel |
| `config.py:15:38` | `'fit_from_bench'` | configurable: unresolved-value sentinel |
| `config.py:15:56` | `'fit_from_pilot'` | configurable: unresolved-value sentinel |
| `config.py:15:74` | `'pin_required'` | configurable: unresolved-value sentinel |
| `config.py:16:23` | `'deploy_required'` | configurable: unresolved-value sentinel |
| `config.py:16:42` | `'design_required'` | configurable: unresolved-value sentinel |
| `config.py:16:61` | `'ask_user'` | configurable: unresolved-value sentinel |
| `config.py:17:24` | `128` | safety-bound: structural index or guard |
| `config.py:26:5` | `'CON'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:26:12` | `'PRN'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:26:19` | `'AUX'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:26:26` | `'NUL'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:26:33` | `'CONIN$'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:26:43` | `'CONOUT$'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:27:47` | `'COM'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:27:54` | `'LPT'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:27:75` | `'0123456789¹²³'` | protocol-bound: reserved Windows name; https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file |
| `config.py:32:4` | `'kb status'` | artifact-bound: command name |
| `config.py:32:18` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:33:4` | `'kb sync'` | artifact-bound: command name |
| `config.py:33:16` | `'knowledge'` | configurable: command read-set key |
| `config.py:33:29` | `'sync'` | configurable: command read-set key |
| `config.py:33:37` | `'embedding'` | configurable: command read-set key |
| `config.py:33:50` | `'observe'` | configurable: command read-set key |
| `config.py:33:61` | `'cards'` | configurable: command read-set key |
| `config.py:33:70` | `'aliases'` | configurable: command read-set key |
| `config.py:33:81` | `'index'` | configurable: command read-set key |
| `config.py:33:90` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:34:16` | `'vlm.roles.observer'` | configurable: command read-set key |
| `config.py:34:38` | `'vlm.roles.info_assessor'` | configurable: command read-set key |
| `config.py:34:65` | `'vlm.roles.folder_namer'` | configurable: command read-set key |
| `config.py:35:16` | `'vlm.roles.card_builder'` | configurable: command read-set key |
| `config.py:35:42` | `'vlm.roles.alias_generator'` | configurable: command read-set key |
| `config.py:35:71` | `'vision'` | configurable: command read-set key |
| `config.py:36:4` | `'kb rollback'` | artifact-bound: command name |
| `config.py:36:20` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:37:4` | `'kb gc'` | artifact-bound: command name |
| `config.py:37:14` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:37:34` | `'sync.gc.retention'` | configurable: command read-set key |
| `config.py:37:55` | `'sync.snapshots.keep'` | configurable: command read-set key |
| `config.py:38:4` | `'kb audit'` | artifact-bound: command name |
| `config.py:38:17` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:39:4` | `'kb show'` | artifact-bound: command name |
| `config.py:39:16` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:40:4` | `'profile register'` | artifact-bound: command name |
| `config.py:40:25` | `'profiles.root'` | configurable: command read-set key |
| `config.py:40:42` | `'vlm.roles.manifest_drafter'` | configurable: command read-set key |
| `config.py:41:4` | `'profile check'` | artifact-bound: command name |
| `config.py:41:22` | `'profiles.root'` | configurable: command read-set key |
| `config.py:42:4` | `'enhance once'` | artifact-bound: command name |
| `config.py:42:21` | `'profiles'` | configurable: command read-set key |
| `config.py:42:33` | `'rag'` | configurable: command read-set key |
| `config.py:42:40` | `'vision'` | configurable: command read-set key |
| `config.py:42:50` | `'candidates'` | configurable: command read-set key |
| `config.py:42:64` | `'embedding'` | configurable: command read-set key |
| `config.py:42:77` | `'planner'` | configurable: command read-set key |
| `config.py:42:88` | `'resolution'` | configurable: command read-set key |
| `config.py:43:21` | `'clarification'` | configurable: command read-set key |
| `config.py:43:38` | `'creativity'` | configurable: command read-set key |
| `config.py:43:52` | `'edit'` | configurable: command read-set key |
| `config.py:43:60` | `'assembly'` | configurable: command read-set key |
| `config.py:43:72` | `'writer'` | configurable: command read-set key |
| `config.py:43:82` | `'validation'` | configurable: command read-set key |
| `config.py:43:96` | `'runtime'` | configurable: command read-set key |
| `config.py:44:21` | `'storage.data_root'` | configurable: command read-set key |
| `config.py:44:42` | `'vlm.roles.planner'` | configurable: command read-set key |
| `config.py:44:63` | `'vlm.roles.writer'` | configurable: command read-set key |
| `config.py:44:83` | `'vlm.roles.verifier'` | configurable: command read-set key |
| `config.py:45:4` | `'enhance serve'` | artifact-bound: command name |
| `config.py:45:22` | `'profiles'` | configurable: command read-set key |
| `config.py:45:34` | `'rag'` | configurable: command read-set key |
| `config.py:45:41` | `'vision'` | configurable: command read-set key |
| `config.py:45:51` | `'candidates'` | configurable: command read-set key |
| `config.py:45:65` | `'embedding'` | configurable: command read-set key |
| `config.py:45:78` | `'planner'` | configurable: command read-set key |
| `config.py:45:89` | `'resolution'` | configurable: command read-set key |
| `config.py:46:22` | `'clarification'` | configurable: command read-set key |
| `config.py:46:39` | `'creativity'` | configurable: command read-set key |
| `config.py:46:53` | `'edit'` | configurable: command read-set key |
| `config.py:46:61` | `'assembly'` | configurable: command read-set key |
| `config.py:46:73` | `'writer'` | configurable: command read-set key |
| `config.py:46:83` | `'validation'` | configurable: command read-set key |
| `config.py:46:97` | `'runtime'` | configurable: command read-set key |
| `config.py:47:22` | `'storage.data_root'` | configurable: command read-set key |
| `config.py:47:43` | `'vlm.roles.planner'` | configurable: command read-set key |
| `config.py:47:64` | `'vlm.roles.writer'` | configurable: command read-set key |
| `config.py:47:84` | `'vlm.roles.verifier'` | configurable: command read-set key |
| `config.py:47:106` | `'serving'` | configurable: command read-set key |
| `config.py:48:4` | `'eval build'` | artifact-bound: command name |
| `config.py:48:19` | `'evaluation'` | configurable: command read-set key |
| `config.py:48:33` | `'storage.kb_store'` | configurable: command read-set key |
| `config.py:48:53` | `'vlm.roles.scenario_generator'` | configurable: command read-set key |
| `config.py:49:4` | `'eval run'` | artifact-bound: command name |
| `config.py:49:17` | `'evaluation'` | configurable: command read-set key |
| `config.py:49:31` | `'profiles'` | configurable: command read-set key |
| `config.py:49:43` | `'rag'` | configurable: command read-set key |
| `config.py:49:50` | `'vision'` | configurable: command read-set key |
| `config.py:49:60` | `'candidates'` | configurable: command read-set key |
| `config.py:49:74` | `'embedding'` | configurable: command read-set key |
| `config.py:49:87` | `'planner'` | configurable: command read-set key |
| `config.py:49:98` | `'resolution'` | configurable: command read-set key |
| `config.py:50:17` | `'clarification'` | configurable: command read-set key |
| `config.py:50:34` | `'creativity'` | configurable: command read-set key |
| `config.py:50:48` | `'edit'` | configurable: command read-set key |
| `config.py:50:56` | `'assembly'` | configurable: command read-set key |
| `config.py:50:68` | `'writer'` | configurable: command read-set key |
| `config.py:50:78` | `'validation'` | configurable: command read-set key |
| `config.py:50:92` | `'runtime'` | configurable: command read-set key |
| `config.py:51:17` | `'storage.data_root'` | configurable: command read-set key |
| `config.py:51:38` | `'vlm.roles.planner'` | configurable: command read-set key |
| `config.py:51:59` | `'vlm.roles.writer'` | configurable: command read-set key |
| `config.py:51:79` | `'vlm.roles.verifier'` | configurable: command read-set key |
| `config.py:52:4` | `'eval fit'` | artifact-bound: command name |
| `config.py:52:17` | `'evaluation'` | configurable: command read-set key |
| `config.py:52:31` | `'storage.data_root'` | configurable: command read-set key |
| `config.py:62:50` | `'\x00'` | safety-bound: path, filename or decoding guard |
| `config.py:64:26` | `'\\'` | safety-bound: path, filename or decoding guard |
| `config.py:64:32` | `'/'` | safety-bound: path, filename or decoding guard |
| `config.py:64:43` | `'/'` | safety-bound: path, filename or decoding guard |
| `config.py:68:20` | `'.'` | safety-bound: path, filename or decoding guard |
| `config.py:68:25` | `'..'` | safety-bound: path, filename or decoding guard |
| `config.py:72:20` | `0` | safety-bound: structural index or guard |
| `config.py:72:39` | `2` | safety-bound: structural index or guard |
| `config.py:72:50` | `0` | safety-bound: structural index or guard |
| `config.py:72:72` | `1` | safety-bound: structural index or guard |
| `config.py:72:78` | `':'` | safety-bound: path, filename or decoding guard |
| `config.py:74:26` | `'.'` | safety-bound: path, filename or decoding guard |
| `config.py:74:31` | `1` | safety-bound: structural index or guard |
| `config.py:74:34` | `0` | safety-bound: structural index or guard |
| `config.py:74:44` | `' '` | safety-bound: path, filename or decoding guard |
| `config.py:75:49` | `'.'` | safety-bound: path, filename or decoding guard |
| `config.py:85:27` | `0` | safety-bound: structural index or guard |
| `config.py:97:52` | `0` | safety-bound: structural index or guard |
| `config.py:104:22` | `'nt'` | safety-bound: path, filename or decoding guard |
| `config.py:104:47` | `'\\\\?\\UNC\\'` | safety-bound: path, filename or decoding guard |
| `config.py:105:19` | `'\\\\'` | safety-bound: path, filename or decoding guard |
| `config.py:105:33` | `8` | safety-bound: structural index or guard |
| `config.py:106:24` | `'nt'` | safety-bound: path, filename or decoding guard |
| `config.py:106:49` | `'\\\\?\\'` | safety-bound: path, filename or decoding guard |
| `config.py:107:24` | `4` | safety-bound: structural index or guard |
| `config.py:117:48` | `'path'` | safety-bound: path, filename or decoding guard |
| `config.py:118:61` | `'.secrets'` | safety-bound: path, filename or decoding guard |
| `config.py:118:81` | `'path'` | safety-bound: path, filename or decoding guard |
| `config.py:136:35` | `'tag:yaml.org,2002:merge'` | protocol-bound: YAML merge tag; https://yaml.org/type/merge.html |
| `config.py:171:44` | `'schema_id'` | safety-bound: path, filename or decoding guard |
| `config.py:174:71` | `'abcdefghijklmnopqrstuvwxyz0123456789_.'` | safety-bound: path, filename or decoding guard |
| `config.py:177:30` | `'contracts'` | artifact-bound: local schema layout |
| `config.py:177:44` | `'schemas'` | artifact-bound: local schema layout |
| `config.py:177:69` | `'.schema.json'` | artifact-bound: local schema layout |
| `config.py:179:66` | `2` | safety-bound: structural index or guard |
| `config.py:193:29` | `'.'` | safety-bound: path, filename or decoding guard |
| `config.py:193:34` | `0` | safety-bound: structural index or guard |
| `config.py:198:50` | `'\x00'` | safety-bound: path, filename or decoding guard |
| `config.py:200:51` | `'nt'` | safety-bound: path, filename or decoding guard |
| `config.py:202:34` | `'\\'` | safety-bound: path, filename or decoding guard |
| `config.py:202:40` | `'/'` | safety-bound: path, filename or decoding guard |
| `config.py:220:27` | `'none'` | configurable: optional credential enum |
| `config.py:227:24` | `2` | safety-bound: structural index or guard |
| `config.py:227:35` | `0` | safety-bound: structural index or guard |
| `config.py:227:41` | `'.secrets'` | safety-bound: path, filename or decoding guard |
| `config.py:230:53` | `'.secrets'` | safety-bound: path, filename or decoding guard |
| `config.py:230:65` | `'.git'` | safety-bound: path, filename or decoding guard |
| `config.py:230:73` | `'.venv'` | safety-bound: path, filename or decoding guard |
| `config.py:232:39` | `'models'` | safety-bound: path, filename or decoding guard |
| `config.py:237:40` | `0` | safety-bound: structural index or guard |
| `config.py:245:17` | `'.'` | safety-bound: path, filename or decoding guard |
| `config.py:245:22` | `'..'` | safety-bound: path, filename or decoding guard |
| `config.py:245:56` | `'/'` | safety-bound: path, filename or decoding guard |
| `config.py:245:61` | `'\\'` | safety-bound: path, filename or decoding guard |
| `config.py:245:67` | `':'` | safety-bound: path, filename or decoding guard |
| `config.py:250:16` | `'knowledge.root'` | configurable: configured path key or enum |
| `config.py:250:34` | `'storage.data_root'` | configurable: configured path key or enum |
| `config.py:250:55` | `'storage.kb_store'` | configurable: configured path key or enum |
| `config.py:250:75` | `'profiles.root'` | configurable: configured path key or enum |
| `config.py:256:27` | `'knowledge'` | configurable: configured path key or enum |
| `config.py:258:14` | `'knowledge.overrides_file'` | configurable: configured path key or enum |
| `config.py:259:11` | `'overrides_file'` | configurable: configured path key or enum |
| `config.py:260:30` | `'overrides_file'` | configurable: configured path key or enum |
| `config.py:261:24` | `'none'` | configurable: configured path key or enum |
| `config.py:263:29` | `'info_file'` | configurable: configured path key or enum |
| `config.py:264:58` | `'names'` | configurable: configured path key or enum |
| `config.py:265:47` | `'names'` | configurable: configured path key or enum |
| `config.py:266:24` | `'knowledge.info_file.names.'` | configurable: configured path key or enum |
| `config.py:268:26` | `'profiles'` | configurable: configured path key or enum |
| `config.py:270:14` | `'profiles.default_profile_id'` | configurable: configured path key or enum |
| `config.py:271:11` | `'default_profile_id'` | configurable: configured path key or enum |
| `config.py:272:31` | `'default_profile_id'` | configurable: configured path key or enum |
| `config.py:273:35` | `'exclude'` | configurable: configured path key or enum |
| `config.py:274:51` | `'exclude'` | configurable: configured path key or enum |
| `config.py:275:24` | `'profiles.exclude.'` | configurable: configured path key or enum |
| `config.py:277:21` | `'vlm'` | configurable: configured path key or enum |
| `config.py:278:23` | `'backends'` | configurable: configured path key or enum |
| `config.py:283:20` | `'vlm.backends.'` | configurable: configured path key or enum |
| `config.py:283:39` | `'.api_key_file'` | configurable: configured path key or enum |
| `config.py:284:15` | `'api_key_file'` | configurable: configured path key or enum |
| `config.py:285:41` | `'api_key_file'` | configurable: configured path key or enum |
| `config.py:286:32` | `'model'` | configurable: configured path key or enum |
| `config.py:288:30` | `'file'` | configurable: configured path key or enum |
| `config.py:288:38` | `'mmproj_file'` | configurable: configured path key or enum |
| `config.py:289:28` | `'vlm.backends.'` | configurable: configured path key or enum |
| `config.py:289:47` | `'.model.'` | configurable: configured path key or enum |
| `config.py:290:58` | `'none'` | configurable: configured path key or enum |
| `config.py:294:46` | `''` | safety-bound: source/diagnostic path assembly |
| `config.py:296:14` | `'.'` | safety-bound: source/diagnostic path assembly |
| `config.py:302:42` | `''` | safety-bound: source/diagnostic path assembly |
| `config.py:312:30` | `'enum'` | protocol-bound: JSON Schema keyword; https://json-schema.org/draft/2020-12/json-schema-core |
| `config.py:314:93` | `'required'` | protocol-bound: JSON Schema keyword; https://json-schema.org/draft/2020-12/json-schema-core |
| `config.py:316:30` | `'required'` | protocol-bound: JSON Schema keyword; https://json-schema.org/draft/2020-12/json-schema-core |
| `config.py:319:37` | `0` | safety-bound: structural index or guard |
| `config.py:320:30` | `'additionalProperties'` | protocol-bound: JSON Schema keyword; https://json-schema.org/draft/2020-12/json-schema-core |
| `config.py:321:70` | `'properties'` | protocol-bound: JSON Schema keyword; https://json-schema.org/draft/2020-12/json-schema-core |
| `config.py:323:35` | `0` | safety-bound: structural index or guard |
| `config.py:324:14` | `'.'` | safety-bound: source/diagnostic path assembly |
| `config.py:325:14` | `'.'` | safety-bound: source/diagnostic path assembly |
| `config.py:325:66` | `'root'` | safety-bound: source/diagnostic path assembly |
| `config.py:345:21` | `'schema_version'` | artifact-bound: configuration file format key |
| `config.py:345:59` | `'schema_version'` | artifact-bound: configuration file format key |
| `config.py:345:80` | `1` | artifact-bound: schema version |
| `config.py:347:7` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:347:29` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:349:7` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:351:14` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:351:27` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:351:50` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:357:58` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:357:69` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:370:15` | `'schema_version'` | artifact-bound: configuration file format key |
| `config.py:370:33` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:370:47` | `'status'` | configurable: deployment setting key |
| `config.py:370:57` | `'storage'` | configurable: deployment setting key |
| `config.py:370:68` | `'runtime'` | configurable: deployment setting key |
| `config.py:370:79` | `'serving'` | configurable: deployment setting key |
| `config.py:370:90` | `'vlm'` | configurable: deployment setting key |
| `config.py:373:60` | `0` | safety-bound: structural index or guard |
| `config.py:374:22` | `'vlm'` | configurable: deployment setting key |
| `config.py:375:48` | `'backends'` | configurable: deployment setting key |
| `config.py:377:23` | `'backends'` | configurable: deployment setting key |
| `config.py:383:34` | `'provider'` | configurable: deployment setting key |
| `config.py:383:46` | `'base_urls'` | configurable: deployment setting key |
| `config.py:383:59` | `'model'` | configurable: deployment setting key |
| `config.py:383:68` | `'extra_body'` | configurable: deployment setting key |
| `config.py:383:82` | `'max_concurrency'` | configurable: deployment setting key |
| `config.py:383:101` | `'endpoints'` | configurable: deployment setting key |
| `config.py:384:34` | `'http'` | configurable: deployment setting key |
| `config.py:384:42` | `'probe'` | configurable: deployment setting key |
| `config.py:384:51` | `'image'` | configurable: deployment setting key |
| `config.py:384:60` | `'anthropic_version'` | configurable: deployment setting key |
| `config.py:386:84` | `0` | safety-bound: structural index or guard |
| `config.py:397:18` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:397:31` | `'extends'` | artifact-bound: configuration file format key |
| `config.py:397:54` | `'overlay_of'` | artifact-bound: configuration file format key |
| `config.py:412:42` | `'.'` | safety-bound: diagnostic key path assembly |
| `config.py:415:42` | `'.'` | safety-bound: diagnostic key path assembly |
| `config.py:420:26` | `'.'` | safety-bound: diagnostic key path assembly |
| `config.py:429:23` | `'enhance once'` | artifact-bound: command name |
| `config.py:429:39` | `'enhance serve'` | artifact-bound: command name |
| `config.py:429:56` | `'eval run'` | artifact-bound: command name |
| `config.py:431:29` | `'rag'` | configurable: read-set key |
| `config.py:433:14` | `'rag'` | configurable: read-set key |
| `config.py:433:21` | `'enabled'` | configurable: read-set key |
| `config.py:434:23` | `'storage.kb_store'` | configurable: read-set key |
| `config.py:435:17` | `'rag.enabled'` | configurable: read-set key |
| `config.py:435:41` | `'rag'` | configurable: read-set key |
| `config.py:436:32` | `'candidates'` | configurable: read-set key |
| `config.py:436:46` | `'embedding'` | configurable: read-set key |
| `config.py:436:59` | `'assembly'` | configurable: read-set key |
| `config.py:444:17` | `'vlm'` | configurable: model setting key or enum |
| `config.py:445:19` | `'backends'` | configurable: model setting key or enum |
| `config.py:446:18` | `'default_backend'` | configurable: model setting key or enum |
| `config.py:451:45` | `'provider'` | configurable: model setting key or enum |
| `config.py:451:84` | `'provider'` | configurable: model setting key or enum |
| `config.py:451:99` | `'openai_compatible'` | configurable: model setting key or enum |
| `config.py:454:49` | `'provider'` | configurable: model setting key or enum |
| `config.py:454:65` | `'anthropic'` | configurable: model setting key or enum |
| `config.py:454:78` | `'gemini'` | configurable: model setting key or enum |
| `config.py:455:34` | `'base_urls'` | configurable: model setting key or enum |
| `config.py:455:58` | `'provider_default'` | configurable: model setting key or enum |
| `config.py:455:88` | `'base_urls'` | configurable: model setting key or enum |
| `config.py:459:35` | `'base_urls'` | configurable: model setting key or enum |
| `config.py:459:57` | `'endpoints'` | configurable: model setting key or enum |
| `config.py:460:26` | `'chat'` | configurable: model setting key or enum |
| `config.py:460:70` | `'chat'` | safety-bound: sentinel counterpart placeholder |
| `config.py:462:20` | `'chat'` | safety-bound: sentinel counterpart placeholder |
| `config.py:471:53` | `'chat'` | configurable: model setting key or enum |
| `config.py:473:29` | `'https://localhost'` | safety-bound: sentinel counterpart placeholder |
| `config.py:473:60` | `'chat'` | configurable: model setting key or enum |
| `config.py:477:26` | `'roles'` | configurable: model setting key or enum |
| `config.py:480:16` | `'vlm.roles.'` | safety-bound: diagnostic key path assembly |
| `config.py:481:24` | `'sampling'` | configurable: model setting key or enum |
| `config.py:486:21` | `'backend'` | configurable: model setting key or enum |
| `config.py:492:52` | `'provider'` | configurable: model setting key or enum |
| `config.py:494:27` | `'provider'` | configurable: model setting key or enum |
| `config.py:495:25` | `'reasoning'` | configurable: model setting key or enum |
| `config.py:496:23` | `'max_tokens'` | configurable: model setting key or enum |
| `config.py:497:55` | `'anthropic'` | configurable: model setting key or enum |
| `config.py:497:81` | `'mode'` | configurable: model setting key or enum |
| `config.py:497:92` | `'on'` | configurable: model setting key or enum |
| `config.py:498:31` | `'budget_tokens'` | configurable: model setting key or enum |
| `config.py:500:55` | `1024` | protocol-bound: Anthropic minimum; https://platform.claude.com/docs/en/api/http/messages/create |
| `config.py:502:22` | `'output'` | configurable: model setting key or enum |
| `config.py:503:47` | `'format'` | configurable: model setting key or enum |
| `config.py:503:60` | `'json_schema'` | configurable: model setting key or enum |
| `config.py:503:85` | `'schema_id'` | configurable: model setting key or enum |
| `config.py:503:102` | `'none'` | configurable: model setting key or enum |
| `config.py:503:110` | `'from_profile_manifest'` | configurable: model setting key or enum |
| `config.py:505:53` | `'anthropic'` | configurable: model setting key or enum |
| `config.py:505:66` | `'gemini'` | configurable: model setting key or enum |
| `config.py:506:25` | `'format'` | configurable: model setting key or enum |
| `config.py:507:47` | `'none'` | configurable: model setting key or enum |
| `config.py:509:21` | `'image'` | configurable: model setting key or enum |
| `config.py:511:27` | `'gemini'` | configurable: model setting key or enum |
| `config.py:511:48` | `'allow_images'` | configurable: model setting key or enum |
| `config.py:513:24` | `'format'` | configurable: model setting key or enum |
| `config.py:514:22` | `'from_vision'` | configurable: model setting key or enum |
| `config.py:515:32` | `'vision'` | configurable: model setting key or enum |
| `config.py:516:33` | `'format'` | configurable: model setting key or enum |
| `config.py:523:65` | `2` | artifact-bound: project layout |
| `config.py:532:25` | `'schema_version'` | configurable: model setting key or enum |
| `config.py:532:63` | `'schema_version'` | configurable: model setting key or enum |
| `config.py:532:84` | `1` | artifact-bound: schema version |
| `config.py:535:23` | `'overlay_of'` | configurable: model setting key or enum |
| `config.py:538:46` | `'overlay_of'` | configurable: model setting key or enum |
| `config.py:544:53` | `': overlay_of:'` | safety-bound: diagnostic key path assembly |
| `config.py:547:53` | `'overlay_of'` | configurable: model setting key or enum |
| `config.py:551:38` | `'enhancer_config.v0'` | artifact-bound: versioned schema id |
| `config.py:556:32` | `'vlm'` | configurable: model setting key or enum |
| `config.py:556:39` | `'backends'` | configurable: model setting key or enum |
| `config.py:559:23` | `'http'` | configurable: model setting key or enum |
| `config.py:560:24` | `'http'` | configurable: model setting key or enum |
| `config.py:560:32` | `'probe'` | configurable: model setting key or enum |
| `config.py:564:32` | `'backoff_initial_s'` | configurable: model setting key or enum |
| `config.py:564:59` | `'backoff_max_s'` | configurable: model setting key or enum |
| `config.py:568:29` | `'vlm'` | configurable: model setting key or enum |
| `config.py:568:36` | `'roles'` | configurable: model setting key or enum |
| `config.py:569:65` | `'output'` | configurable: model setting key or enum |
| `config.py:571:25` | `'output'` | configurable: model setting key or enum |
| `config.py:571:39` | `'schema_id'` | configurable: model setting key or enum |
| `config.py:572:60` | `'none'` | configurable: model setting key or enum |
| `config.py:572:68` | `'from_profile_manifest'` | configurable: model setting key or enum |
| `config.py:576:24` | `'vlm.roles.'` | safety-bound: diagnostic key path assembly |
| `config.py:576:40` | `'.output.schema_id'` | safety-bound: diagnostic key path assembly |
| `config.py:582:26` | `'vlm.roles.'` | configurable: model setting key or enum |
| `config.py:584:44` | `'vlm_role.v0'` | artifact-bound: versioned schema id |
| `config.py:588:26` | `'vlm.backends.'` | configurable: model setting key or enum |
| `config.py:588:50` | `'backend'` | configurable: model setting key or enum |
| `config.py:590:26` | `'vlm.roles.'` | configurable: model setting key or enum |
| `config.py:591:25` | `'vlm'` | configurable: model setting key or enum |
| `config.py:591:32` | `'default_backend'` | configurable: model setting key or enum |
| `config.py:592:33` | `'vlm.default_backend'` | configurable: model setting key or enum |
| `config.py:593:22` | `'vlm.backends.'` | configurable: model setting key or enum |
| `config.py:600:65` | `2` | artifact-bound: project layout |
| `config.py:602:35` | `'manifest.yaml'` | artifact-bound: profile artifact name |
| `config.py:604:25` | `'schema_version'` | configurable: profile or writer key or enum |
| `config.py:604:67` | `'schema_version'` | configurable: profile or writer key or enum |
| `config.py:604:88` | `1` | artifact-bound: schema version |
| `config.py:607:40` | `'profile_manifest.v0'` | artifact-bound: profile artifact name |
| `config.py:611:23` | `'profile_id'` | configurable: profile or writer key or enum |
| `config.py:611:53` | `'profile_id'` | configurable: profile or writer key or enum |
| `config.py:613:37` | `'system_prompt.txt'` | artifact-bound: profile artifact name |
| `config.py:620:76` | `'config.yaml'` | artifact-bound: profile artifact name |
| `config.py:620:107` | `'manifest.yaml'` | artifact-bound: profile artifact name |
| `config.py:622:65` | `2` | artifact-bound: project layout |
| `config.py:624:24` | `'output'` | configurable: profile or writer key or enum |
| `config.py:625:18` | `'format'` | configurable: profile or writer key or enum |
| `config.py:625:31` | `'from_profile_manifest'` | configurable: profile or writer key or enum |
| `config.py:625:69` | `'schema_id'` | configurable: profile or writer key or enum |
| `config.py:625:85` | `'from_profile_manifest'` | configurable: profile or writer key or enum |
| `config.py:627:29` | `'output_schema_id'` | configurable: profile or writer key or enum |
| `config.py:630:20` | `'none'` | configurable: profile or writer key or enum |
| `config.py:635:11` | `'schema_id'` | configurable: profile or writer key or enum |
| `config.py:636:11` | `'format'` | configurable: profile or writer key or enum |
| `config.py:636:23` | `'none'` | configurable: profile or writer key or enum |
| `config.py:636:46` | `'none'` | configurable: profile or writer key or enum |
| `config.py:636:58` | `'json_schema'` | configurable: profile or writer key or enum |
| `model_client.py:33:31` | `'vlm'` | configurable: model profile key |
| `model_client.py:33:38` | `'backends'` | configurable: model profile key |
| `model_client.py:34:42` | `'image'` | configurable: model profile key |
| `model_client.py:34:51` | `'max_decode_pixels'` | configurable: model profile key |
| `model_client.py:35:90` | `'image'` | configurable: model profile key |
| `model_client.py:36:51` | `'image'` | configurable: model profile key |
| `model_client.py:36:60` | `'max_decode_pixels'` | configurable: model profile key |
| `model_client.py:37:61` | `'max_concurrency'` | configurable: model profile key |
| `model_client.py:39:64` | `'max_concurrency'` | configurable: model profile key |
| `model_client.py:40:59` | `'call_budget'` | configurable: model profile key |
| `model_client.py:41:94` | `'call_budget'` | configurable: model profile key |
| `model_client.py:44:58` | `'contracts'` | artifact-bound: schema directory |
| `model_client.py:44:72` | `'schemas'` | artifact-bound: schema directory |
| `model_client.py:50:27` | `'vlm'` | configurable: model profile key |
| `model_client.py:50:34` | `'roles'` | configurable: model profile key |
| `model_client.py:51:45` | `'vlm.roles.'` | safety-bound: configuration refusal path label |
| `model_client.py:52:21` | `'image'` | configurable: model profile key |
| `model_client.py:54:33` | `'vision'` | configurable: model profile key |
| `model_client.py:56:53` | `'vision'` | safety-bound: configuration refusal path label |
| `model_client.py:57:25` | `'max_side'` | configurable: model profile key |
| `model_client.py:57:37` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:57:55` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:58:33` | `'max_image_side'` | configurable: model profile key |
| `model_client.py:58:71` | `'vision.max_image_side'` | safety-bound: configuration refusal path label |
| `model_client.py:59:21` | `'format'` | configurable: model profile key |
| `model_client.py:59:34` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:60:33` | `'format'` | configurable: model profile key |
| `model_client.py:60:63` | `'vision.format'` | safety-bound: configuration refusal path label |
| `model_client.py:61:30` | `'vlm'` | configurable: model profile key |
| `model_client.py:61:37` | `'default_backend'` | configurable: model profile key |
| `model_client.py:62:46` | `'vlm.default_backend'` | safety-bound: configuration refusal path label |
| `model_client.py:63:26` | `'backend'` | configurable: model profile key |
| `model_client.py:64:34` | `'vlm'` | configurable: model profile key |
| `model_client.py:64:41` | `'backends'` | configurable: model profile key |
| `model_client.py:65:52` | `'vlm.backends.'` | safety-bound: configuration refusal path label |
| `model_client.py:68:16` | `'backend'` | configurable: model profile key |
| `model_client.py:70:53` | `'backend'` | configurable: model profile key |
| `model_client.py:72:31` | `'vlm'` | configurable: model profile key |
| `model_client.py:72:38` | `'backends'` | configurable: model profile key |
| `model_client.py:72:55` | `'backend'` | configurable: model profile key |
| `model_client.py:72:71` | `'on_budget_exhausted'` | configurable: model profile key |
| `model_client.py:72:97` | `'fallback_to_default_backend'` | configurable: provider or role enum |
| `model_client.py:109:57` | `','` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:109:62` | `':'` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:110:38` | `'utf-8'` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:114:24` | `'usageMetadata'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:114:55` | `'gemini'` | configurable: provider or role enum |
| `model_client.py:114:69` | `'usage'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:117:14` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:117:36` | `'prompt_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:117:53` | `'completion_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:117:74` | `'total_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:118:14` | `'anthropic'` | configurable: provider or role enum |
| `model_client.py:118:28` | `'input_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:118:44` | `'output_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:119:14` | `'gemini'` | configurable: provider or role enum |
| `model_client.py:119:25` | `'promptTokenCount'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:119:45` | `'candidatesTokenCount'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:120:95` | `0` | safety-bound: structural index or guard |
| `model_client.py:137:30` | `'vlm'` | configurable: model profile key |
| `model_client.py:137:37` | `'backends'` | configurable: model profile key |
| `model_client.py:139:23` | `'provider'` | configurable: model profile key |
| `model_client.py:139:38` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:142:40` | `'probe'` | configurable: model profile key |
| `model_client.py:143:31` | `'endpoints'` | configurable: model profile key |
| `model_client.py:143:44` | `'chat'` | configurable: model profile key |
| `model_client.py:144:76` | `'image'` | artifact-bound: capability or probe state |
| `model_client.py:145:53` | `2` | safety-bound: structural index or guard |
| `model_client.py:145:63` | `'max_requests'` | configurable: model profile key |
| `model_client.py:145:88` | `'timeout_s'` | configurable: model profile key |
| `model_client.py:146:31` | `'max_requests'` | configurable: model profile key |
| `model_client.py:146:53` | `2` | safety-bound: structural index or guard |
| `model_client.py:146:63` | `'max_requests'` | configurable: model profile key |
| `model_client.py:148:86` | `'thinking'` | safety-bound: probe/transport interface member |
| `model_client.py:149:64` | `2` | safety-bound: structural index or guard |
| `model_client.py:149:86` | `'timeout_s'` | configurable: model profile key |
| `model_client.py:151:39` | `'incomplete'` | artifact-bound: capability or probe state |
| `model_client.py:152:22` | `'failed'` | artifact-bound: capability or probe state |
| `model_client.py:152:34` | `'failed'` | artifact-bound: capability or probe state |
| `model_client.py:153:22` | `'incomplete'` | artifact-bound: capability or probe state |
| `model_client.py:153:38` | `'incomplete'` | artifact-bound: capability or probe state |
| `model_client.py:153:91` | `'complete'` | artifact-bound: capability or probe state |
| `model_client.py:161:19` | `'provider'` | configurable: model profile key |
| `model_client.py:161:34` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:162:54` | `'ledger'` | safety-bound: probe/transport interface member |
| `model_client.py:166:44` | `'provider'` | configurable: model profile key |
| `model_client.py:167:25` | `'probe'` | configurable: model profile key |
| `model_client.py:168:76` | `'endpoints'` | configurable: model profile key |
| `model_client.py:168:89` | `'chat'` | configurable: model profile key |
| `model_client.py:169:76` | `'max_requests'` | configurable: model profile key |
| `model_client.py:169:100` | `'timeout_s'` | configurable: model profile key |
| `model_client.py:175:36` | `'vlm'` | configurable: model profile key |
| `model_client.py:175:43` | `'roles'` | configurable: model profile key |
| `model_client.py:176:24` | `'writer'` | artifact-bound: role or image role; contracts/vlm-request.md:24-34,55-57 |
| `model_client.py:180:28` | `'backend'` | configurable: model profile key |
| `model_client.py:183:34` | `'vlm'` | configurable: model profile key |
| `model_client.py:183:41` | `'backends'` | configurable: model profile key |
| `model_client.py:184:31` | `'provider'` | configurable: model profile key |
| `model_client.py:186:31` | `'image'` | configurable: model profile key |
| `model_client.py:186:43` | `'none'` | configurable: provider or role enum |
| `model_client.py:188:38` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:188:74` | `'allow_images'` | configurable: model profile key |
| `model_client.py:189:39` | `'vlm'` | configurable: model profile key |
| `model_client.py:189:46` | `'default_backend'` | configurable: model profile key |
| `model_client.py:195:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:197:59` | `'ledger'` | safety-bound: probe/transport interface member |
| `model_client.py:201:42` | `'gemini'` | configurable: provider or role enum |
| `model_client.py:208:35` | `'on_budget_exhausted'` | configurable: model profile key |
| `model_client.py:208:61` | `'fallback_to_default_backend'` | configurable: provider or role enum |
| `model_client.py:210:43` | `'vlm'` | configurable: model profile key |
| `model_client.py:210:50` | `'default_backend'` | configurable: model profile key |
| `model_client.py:213:37` | `'BUDGET_EXHAUSTED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:217:26` | `'output'` | configurable: model profile key |
| `model_client.py:218:22` | `'format'` | configurable: model profile key |
| `model_client.py:218:35` | `'none'` | configurable: provider or role enum |
| `model_client.py:218:53` | `'validate_response'` | configurable: model profile key |
| `model_client.py:220:36` | `'format'` | configurable: model profile key |
| `model_client.py:220:49` | `'none'` | configurable: provider or role enum |
| `model_client.py:220:69` | `'format'` | configurable: model profile key |
| `model_client.py:222:32` | `'image'` | artifact-bound: capability or probe state |
| `model_client.py:223:33` | `1` | safety-bound: structural index or guard |
| `model_client.py:224:36` | `'multiple_images'` | artifact-bound: capability or probe state |
| `model_client.py:228:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:230:17` | `'model'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:230:36` | `'model'` | configurable: model profile key |
| `model_client.py:230:45` | `'id'` | configurable: model profile key |
| `model_client.py:234:38` | `'image'` | configurable: model profile key |
| `model_client.py:235:37` | `'vision'` | configurable: model profile key |
| `model_client.py:236:46` | `'max_side'` | configurable: model profile key |
| `model_client.py:236:58` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:237:34` | `'max_image_side'` | configurable: model profile key |
| `model_client.py:237:67` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:238:37` | `'format'` | configurable: model profile key |
| `model_client.py:239:29` | `'format'` | configurable: model profile key |
| `model_client.py:239:49` | `'from_vision'` | configurable: provider or role enum |
| `model_client.py:240:41` | `'quality'` | configurable: model profile key |
| `model_client.py:241:28` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:241:36` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:241:44` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:243:42` | `'source_image'` | artifact-bound: role or image role; contracts/vlm-request.md:24-34,55-57 |
| `model_client.py:243:58` | `'user_reference'` | artifact-bound: role or image role; contracts/vlm-request.md:24-34,55-57 |
| `model_client.py:243:76` | `'knowledge_reference'` | artifact-bound: role or image role; contracts/vlm-request.md:24-34,55-57 |
| `model_client.py:245:75` | `'image'` | configurable: model profile key |
| `model_client.py:245:84` | `'max_decode_pixels'` | configurable: model profile key |
| `model_client.py:247:64` | `'ascii'` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:248:43` | `'image/'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:36` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:44` | `'image_url'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:57` | `'image_url'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:71` | `'url'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:80` | `'data:image/'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:249:96` | `';base64,'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:250:17` | `'messages'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:250:33` | `'role'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:250:41` | `'system'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:250:51` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:250:83` | `'utf-8'` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:251:33` | `'role'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:251:41` | `'user'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:251:49` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:252:29` | `'reasoning'` | configurable: model profile key |
| `model_client.py:253:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:253:61` | `'mode'` | configurable: model profile key |
| `model_client.py:253:73` | `'on'` | configurable: provider or role enum |
| `model_client.py:253:79` | `'off'` | configurable: provider or role enum |
| `model_client.py:254:36` | `'reasoning.mode'` | artifact-bound: capability or probe state |
| `model_client.py:255:25` | `'chat_template_kwargs'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:255:62` | `'template_kwargs_key'` | configurable: model profile key |
| `model_client.py:255:96` | `'mode'` | configurable: model profile key |
| `model_client.py:255:107` | `'on'` | configurable: provider or role enum |
| `model_client.py:257:37` | `'PARAM_UNSUPPORTED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:257:58` | `'reasoning.mode'` | artifact-bound: capability or probe state |
| `model_client.py:258:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:258:61` | `'effort'` | configurable: model profile key |
| `model_client.py:258:79` | `'none'` | configurable: provider or role enum |
| `model_client.py:258:87` | `'backend_default'` | configurable: provider or role enum |
| `model_client.py:259:36` | `'reasoning.effort'` | artifact-bound: capability or probe state |
| `model_client.py:260:25` | `'reasoning_effort'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:260:57` | `'effort'` | configurable: model profile key |
| `model_client.py:262:37` | `'PARAM_UNSUPPORTED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:262:58` | `'reasoning.effort'` | artifact-bound: capability or probe state |
| `model_client.py:263:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:263:61` | `'budget_tokens'` | configurable: model profile key |
| `model_client.py:263:86` | `'none'` | configurable: provider or role enum |
| `model_client.py:264:33` | `'PARAM_UNSUPPORTED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:264:54` | `'reasoning.budget_tokens'` | artifact-bound: capability or probe state |
| `model_client.py:265:31` | `'schema_id'` | configurable: model profile key |
| `model_client.py:268:22` | `'validate_response'` | configurable: model profile key |
| `model_client.py:268:60` | `'none'` | configurable: provider or role enum |
| `model_client.py:270:27` | `'anthropic'` | configurable: provider or role enum |
| `model_client.py:270:50` | `'format'` | configurable: model profile key |
| `model_client.py:270:63` | `'json_schema'` | configurable: provider or role enum |
| `model_client.py:272:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:272:58` | `'format'` | configurable: model profile key |
| `model_client.py:272:71` | `'json_object'` | configurable: provider or role enum |
| `model_client.py:273:21` | `'response_format'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:273:43` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:273:51` | `'json_object'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:274:29` | `'openai_compatible'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:274:60` | `'format'` | configurable: model profile key |
| `model_client.py:274:73` | `'json_schema'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:276:21` | `'response_format'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:276:43` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:276:51` | `'json_schema'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:276:66` | `'json_schema'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:277:20` | `'name'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:277:39` | `'schema'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:277:57` | `'strict'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:277:74` | `'strict'` | configurable: model profile key |
| `model_client.py:278:27` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:279:49` | `'extra_body'` | configurable: model profile key |
| `model_client.py:281:99` | `'utf-8'` | artifact-bound: canonical encoding or data serialization |
| `model_client.py:285:36` | `'body_hash'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:285:60` | `'role'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:285:79` | `'prompt_id'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:285:97` | `'prompt_id'` | configurable: model profile key |
| `model_client.py:286:36` | `'backend'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:286:61` | `'provider'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:287:36` | `'model_revision'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:287:62` | `'model'` | configurable: model profile key |
| `model_client.py:287:71` | `'revision'` | configurable: model profile key |
| `model_client.py:287:84` | `'schema_hash'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:288:36` | `'image_roles'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:289:22` | `'role'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:289:41` | `'prompt_id'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:289:59` | `'prompt_id'` | configurable: model profile key |
| `model_client.py:289:73` | `'backend'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:290:22` | `'provider'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:290:44` | `'model_id'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:290:64` | `'model'` | configurable: model profile key |
| `model_client.py:290:73` | `'id'` | configurable: model profile key |
| `model_client.py:291:22` | `'model_revision'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:291:48` | `'model'` | configurable: model profile key |
| `model_client.py:291:57` | `'revision'` | configurable: model profile key |
| `model_client.py:291:70` | `'schema_id'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:292:22` | `'schema_hash'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:292:50` | `'effective_body_hash'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:292:84` | `'request_hash'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:293:22` | `'image_roles'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:294:22` | `'warnings'` | artifact-bound: request hash or call record; contracts/vlm-request.md:59-61 |
| `model_client.py:297:76` | `'endpoints'` | configurable: model profile key |
| `model_client.py:297:89` | `'chat'` | configurable: model profile key |
| `model_client.py:299:31` | `'on_budget_exhausted'` | configurable: model profile key |
| `model_client.py:299:57` | `'fallback_to_default_backend'` | configurable: provider or role enum |
| `model_client.py:301:39` | `'vlm'` | configurable: model profile key |
| `model_client.py:301:46` | `'default_backend'` | configurable: model profile key |
| `model_client.py:304:33` | `'BUDGET_EXHAUSTED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:309:23` | `'status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:309:35` | `'HTTP_ERROR'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:310:23` | `'error_type'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:313:23` | `'latency_s'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:315:23` | `'status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:315:35` | `'HTTP_ERROR'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:316:23` | `'http_status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:320:31` | `'openai_compatible'` | configurable: provider or role enum |
| `model_client.py:321:37` | `'choices'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:321:48` | `0` | safety-bound: structural index or guard |
| `model_client.py:322:37` | `'message'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:323:51` | `'finish_reason'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:323:77` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:323:101` | `'refusal'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:324:32` | `'stop'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:325:33` | `'anthropic'` | configurable: provider or role enum |
| `model_client.py:326:37` | `'stop_reason'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:327:27` | `''` | safety-bound: response text concatenation |
| `model_client.py:327:40` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:327:68` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:327:87` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:327:98` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:328:40` | `'refusal'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:329:32` | `'end_turn'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:329:44` | `'stop_sequence'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:331:37` | `'candidates'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:331:51` | `0` | safety-bound: structural index or guard |
| `model_client.py:332:36` | `'finishReason'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:333:27` | `''` | safety-bound: response text concatenation |
| `model_client.py:333:40` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:333:67` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:333:78` | `'parts'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:333:90` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:334:40` | `'SAFETY'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:335:32` | `'STOP'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:337:27` | `'status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:337:39` | `'INCOMPLETE_OR_REFUSED'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:338:27` | `'finish_reason'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:338:67` | `'length'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:338:77` | `'content_filter'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:338:95` | `'max_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:339:68` | `'MAX_TOKENS'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:339:82` | `'SAFETY'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_client.py:339:97` | `'other'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:340:27` | `'usage'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:345:26` | `'format'` | configurable: model profile key |
| `model_client.py:345:39` | `'none'` | configurable: provider or role enum |
| `model_client.py:347:26` | `'validate_response'` | configurable: model profile key |
| `model_client.py:347:64` | `'none'` | configurable: provider or role enum |
| `model_client.py:349:23` | `'usage'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:350:23` | `'status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:350:35` | `'OK'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:353:23` | `'status'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:353:35` | `'SCHEMA_INVALID'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_client.py:354:23` | `'warnings'` | artifact-bound: call record; contracts/vlm-request.md:59-61 |
| `model_client.py:354:43` | `'SCHEMA_INVALID'` | artifact-bound: call-record status; contracts/vlm-request.md:10,13,22,59-61 |
| `model_cloud.py:12:45` | `'api_key_file'` | configurable: provider or role/backend setting |
| `model_cloud.py:13:19` | `'anthropic'` | configurable: provider or role/backend setting |
| `model_cloud.py:14:26` | `'anthropic_version'` | configurable: provider or role/backend setting |
| `model_cloud.py:15:22` | `'deploy_required'` | configurable: provider or role/backend setting |
| `model_cloud.py:17:16` | `'Authorization'` | protocol-bound: Anthropic auth; https://platform.claude.com/docs/en/api/overview |
| `model_cloud.py:17:33` | `'Bearer '` | protocol-bound: Anthropic auth; https://platform.claude.com/docs/en/api/overview |
| `model_cloud.py:17:50` | `'anthropic-version'` | protocol-bound: Anthropic auth; https://platform.claude.com/docs/en/api/overview |
| `model_cloud.py:18:19` | `'gemini'` | configurable: provider or role/backend setting |
| `model_cloud.py:20:16` | `'x-goog-api-key'` | protocol-bound for interactions only: https://aistudio.google.com/docs/api-key; generateContent unverified |
| `model_cloud.py:26:19` | `'anthropic'` | configurable: provider or role/backend setting |
| `model_cloud.py:26:40` | `'reasoning'` | configurable: provider or role/backend setting |
| `model_cloud.py:26:53` | `'mode'` | configurable: provider or role/backend setting |
| `model_cloud.py:26:64` | `'on'` | configurable: provider or role/backend setting |
| `model_cloud.py:27:22` | `'reasoning'` | configurable: provider or role/backend setting |
| `model_cloud.py:27:35` | `'budget_tokens'` | configurable: provider or role/backend setting |
| `model_cloud.py:29:47` | `1024` | protocol-bound: Anthropic minimum; https://platform.claude.com/docs/en/api/http/messages/create |
| `model_cloud.py:29:70` | `'max_tokens'` | configurable: provider or role/backend setting |
| `model_cloud.py:32:60` | `'reasoning'` | configurable: provider or role/backend setting |
| `model_cloud.py:34:18` | `'output'` | configurable: provider or role/backend setting |
| `model_cloud.py:35:14` | `'format'` | configurable: provider or role/backend setting |
| `model_cloud.py:35:27` | `'none'` | configurable: provider or role/backend setting |
| `model_cloud.py:37:19` | `'anthropic'` | configurable: provider or role/backend setting |
| `model_cloud.py:40:24` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:40:32` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:40:40` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:32` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:40` | `'image'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:49` | `'source'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:60` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:68` | `'base64'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:42:78` | `'media_type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:43:31` | `'data'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:43:69` | `'ascii'` | safety-bound: base64 encoding |
| `model_cloud.py:44:16` | `'model'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:44:33` | `'model'` | configurable: provider or role/backend setting |
| `model_cloud.py:44:42` | `'id'` | configurable: provider or role/backend setting |
| `model_cloud.py:44:49` | `'system'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:45:16` | `'messages'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:45:30` | `'role'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:45:38` | `'user'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:45:46` | `'content'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:46:21` | `'gemini'` | configurable: provider or role/backend setting |
| `model_cloud.py:50:16` | `'systemInstruction'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:50:38` | `'parts'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:50:49` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:51:16` | `'contents'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:51:30` | `'role'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:51:38` | `'user'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:51:46` | `'parts'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:51:57` | `'text'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:52:16` | `'generationConfig'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_cloud.py:55:37` | `'extra_body'` | configurable: provider or role/backend setting |
| `model_image.py:26:18` | `'max_image_side'` | configurable: vision key or max-side enum |
| `model_image.py:26:36` | `'format'` | configurable: vision key or max-side enum |
| `model_image.py:26:46` | `'quality'` | configurable: vision key or max-side enum |
| `model_image.py:30:38` | `'max_image_side'` | configurable: vision key or max-side enum |
| `model_image.py:31:46` | `'format'` | configurable: vision key or max-side enum |
| `model_image.py:31:72` | `'quality'` | configurable: vision key or max-side enum |
| `model_image.py:36:19` | `'none'` | configurable: vision key or max-side enum |
| `model_image.py:36:71` | `1` | safety-bound: structural index or guard |
| `model_image.py:38:65` | `1` | safety-bound: structural index or guard |
| `model_image.py:43:42` | `274` | protocol-bound: Orientation; https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose |
| `model_image.py:43:47` | `1` | protocol-bound: Orientation; https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose |
| `model_image.py:49:30` | `'none'` | configurable: vision key or max-side enum |
| `model_image.py:51:30` | `1` | safety-bound: structural index or guard |
| `model_image.py:51:65` | `1` | safety-bound: structural index or guard |
| `model_image.py:53:42` | `1` | protocol-bound: Orientation; https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose |
| `model_image.py:58:45` | `'quality'` | protocol-bound: Pillow encoder option; https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg |
| `model_mapping.py:8:4` | `'openai_compatible'` | configurable: provider or role key |
| `model_mapping.py:8:26` | `'temperature'` | configurable: provider or role key |
| `model_mapping.py:8:41` | `'temperature'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:8:56` | `'top_p'` | configurable: provider or role key |
| `model_mapping.py:8:65` | `'top_p'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:9:26` | `'presence_penalty'` | configurable: provider or role key |
| `model_mapping.py:9:46` | `'presence_penalty'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:9:66` | `'frequency_penalty'` | configurable: provider or role key |
| `model_mapping.py:9:87` | `'frequency_penalty'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:10:26` | `'seed'` | configurable: provider or role key |
| `model_mapping.py:10:34` | `'seed'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:10:42` | `'stop'` | configurable: provider or role key |
| `model_mapping.py:10:50` | `'stop'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:11:4` | `'anthropic'` | configurable: provider or role key |
| `model_mapping.py:11:18` | `'stop'` | configurable: provider or role key |
| `model_mapping.py:11:26` | `'stop_sequences'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:12:4` | `'gemini'` | configurable: provider or role key |
| `model_mapping.py:12:15` | `'temperature'` | configurable: provider or role key |
| `model_mapping.py:12:30` | `'temperature'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:12:45` | `'top_p'` | configurable: provider or role key |
| `model_mapping.py:12:54` | `'topP'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:12:62` | `'top_k'` | configurable: provider or role key |
| `model_mapping.py:12:71` | `'topK'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:13:15` | `'presence_penalty'` | configurable: provider or role key |
| `model_mapping.py:13:35` | `'presencePenalty'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:13:54` | `'frequency_penalty'` | configurable: provider or role key |
| `model_mapping.py:13:75` | `'frequencyPenalty'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:14:15` | `'seed'` | configurable: provider or role key |
| `model_mapping.py:14:23` | `'seed'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:14:31` | `'stop'` | configurable: provider or role key |
| `model_mapping.py:14:39` | `'stopSequences'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:22:19` | `'openai_compatible'` | configurable: provider or role setting or enum |
| `model_mapping.py:24:21` | `'top_k'` | configurable: provider or role setting or enum |
| `model_mapping.py:24:30` | `'min_p'` | configurable: provider or role setting or enum |
| `model_mapping.py:25:32` | `'sampling.'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:28:15` | `'openai_compatible'` | configurable: provider or role key |
| `model_mapping.py:28:36` | `'max_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:28:50` | `'anthropic'` | configurable: provider or role key |
| `model_mapping.py:28:63` | `'max_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:28:77` | `'gemini'` | configurable: provider or role key |
| `model_mapping.py:28:87` | `'maxOutputTokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:29:37` | `'max_tokens'` | configurable: provider or role setting or enum |
| `model_mapping.py:30:28` | `'sampling'` | configurable: provider or role setting or enum |
| `model_mapping.py:31:20` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:33:47` | `'openai_compatible'` | configurable: provider or role setting or enum |
| `model_mapping.py:34:56` | `'sampling.'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:35:29` | `'PARAM_UNSUPPORTED'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:35:50` | `'sampling.'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:44:24` | `'anthropic'` | configurable: provider or role setting or enum |
| `model_mapping.py:44:37` | `'gemini'` | configurable: provider or role setting or enum |
| `model_mapping.py:47:37` | `'mode'` | configurable: provider or role setting or enum |
| `model_mapping.py:47:56` | `'effort'` | configurable: provider or role setting or enum |
| `model_mapping.py:47:77` | `'budget_tokens'` | configurable: provider or role setting or enum |
| `model_mapping.py:48:19` | `'anthropic'` | configurable: provider or role setting or enum |
| `model_mapping.py:49:19` | `'on'` | configurable: provider or role setting or enum |
| `model_mapping.py:49:43` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:50:19` | `'thinking'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:50:34` | `'type'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:50:42` | `'enabled'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:50:53` | `'budget_tokens'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:51:21` | `'auto'` | configurable: provider or role setting or enum |
| `model_mapping.py:52:29` | `'PARAM_UNSUPPORTED'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:52:50` | `'reasoning.mode'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:53:26` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:53:34` | `'backend_default'` | configurable: provider or role setting or enum |
| `model_mapping.py:54:19` | `'output_config'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:54:39` | `'effort'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:55:26` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:55:52` | `'on'` | configurable: provider or role setting or enum |
| `model_mapping.py:56:29` | `'PARAM_UNSUPPORTED'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:56:50` | `'reasoning.budget_tokens'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:58:19` | `'auto'` | configurable: provider or role setting or enum |
| `model_mapping.py:59:29` | `'PARAM_UNSUPPORTED'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:59:50` | `'reasoning.mode'` | artifact-bound: warning or capability contract; contracts/vlm-request.md:10-11 |
| `model_mapping.py:61:26` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:61:34` | `'backend_default'` | configurable: provider or role setting or enum |
| `model_mapping.py:62:21` | `'thinkingLevel'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:63:26` | `'none'` | configurable: provider or role setting or enum |
| `model_mapping.py:64:21` | `'thinkingBudget'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_mapping.py:66:19` | `'thinkingConfig'` | protocol-bound: provider field; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create ; https://platform.claude.com/docs/en/api/http/messages/create ; https://aistudio.google.com/api-reference/generate-content |
| `model_probe.py:25:56` | `1` | safety-bound: structural index or guard |
| `model_probe.py:26:60` | `0` | safety-bound: structural index or guard |
| `model_probe.py:41:13` | `'failed'` | artifact-bound: probe result or capability label |
| `model_probe.py:41:38` | `'incomplete'` | artifact-bound: probe result or capability label |
| `model_probe.py:41:90` | `'complete'` | artifact-bound: probe result or capability label |
| `model_probe.py:75:29` | `0` | safety-bound: structural index or guard |
| `model_probe.py:75:50` | `0` | safety-bound: structural index or guard |
| `model_probe.py:76:22` | `1` | safety-bound: structural index or guard |
| `model_probe.py:76:33` | `1` | safety-bound: structural index or guard |
| `model_probe.py:77:69` | `0` | safety-bound: structural index or guard |
| `model_probe.py:77:84` | `1` | safety-bound: structural index or guard |
| `model_probe.py:81:42` | `'messages'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:81:76` | `'content'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:82:37` | `'content'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:82:60` | `'type'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:82:71` | `'image_url'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:83:25` | `1` | safety-bound: structural index or guard |
| `model_probe.py:85:26` | `0` | safety-bound: structural index or guard |
| `model_probe.py:85:29` | `'image_url'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:85:42` | `'url'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:86:14` | `0` | safety-bound: structural index or guard |
| `model_probe.py:86:17` | `'image_url'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:86:30` | `'url'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:86:39` | `'<image>'` | safety-bound: comparison placeholder or challenge label |
| `model_probe.py:87:12` | `0` | safety-bound: structural index or guard |
| `model_probe.py:87:23` | `1` | safety-bound: structural index or guard |
| `model_probe.py:87:36` | `0` | safety-bound: structural index or guard |
| `model_probe.py:87:49` | `1` | safety-bound: structural index or guard |
| `model_probe.py:93:41` | `'choices'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:93:52` | `0` | safety-bound: structural index or guard |
| `model_probe.py:94:26` | `'finish_reason'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:94:46` | `'stop'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:94:63` | `'message'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:94:78` | `'refusal'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:96:43` | `'message'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:96:54` | `'content'` | protocol-bound: OpenAI-compatible response or request; https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create |
| `model_probe.py:97:39` | `'value'` | artifact-bound: probe result or capability label |
| `model_probe.py:97:67` | `'value'` | artifact-bound: probe result or capability label |
| `model_probe.py:102:13` | `'first'` | safety-bound: comparison placeholder or challenge label |
| `model_probe.py:102:29` | `0` | safety-bound: structural index or guard |
| `model_probe.py:102:51` | `0` | safety-bound: structural index or guard |
| `model_probe.py:102:57` | `'second'` | safety-bound: comparison placeholder or challenge label |
| `model_probe.py:102:75` | `0` | safety-bound: structural index or guard |
| `model_probe.py:102:97` | `1` | safety-bound: structural index or guard |
| `model_probe.py:104:24` | `'complete'` | artifact-bound: probe result or capability label |
| `model_probe.py:106:23` | `'complete'` | artifact-bound: probe result or capability label |
| `model_probe.py:106:36` | `'image'` | artifact-bound: probe result or capability label |
| `model_probe.py:117:20` | `'chat_template_kwargs'` | protocol-bound: template switch; https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md |
| `model_probe.py:118:28` | `'chat_template_kwargs'` | protocol-bound: template switch; https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md |
| `model_probe.py:120:12` | `'chat_template_kwargs'` | protocol-bound: template switch; https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md |
| `model_probe.py:132:55` | `'on'` | safety-bound: comparison placeholder or challenge label |
| `model_probe.py:133:32` | `'off'` | safety-bound: comparison placeholder or challenge label |
| `model_probe.py:134:24` | `'complete'` | artifact-bound: probe result or capability label |
| `model_probe.py:136:23` | `'complete'` | artifact-bound: probe result or capability label |
| `model_probe.py:136:36` | `'reasoning.mode'` | artifact-bound: probe result or capability label |
| `model_resources.py:16:17` | `'reserved'` | safety-bound: reservation state |
| `model_resources.py:23:49` | `0` | safety-bound: structural index or guard |
| `model_resources.py:33:43` | `0` | safety-bound: structural index or guard |
| `model_resources.py:35:40` | `1` | safety-bound: structural index or guard |
| `model_resources.py:48:26` | `'committed'` | safety-bound: reservation state |
| `model_resources.py:54:40` | `1` | safety-bound: structural index or guard |
| `model_resources.py:55:26` | `'released'` | safety-bound: reservation state |
| `model_resources.py:62:49` | `1` | safety-bound: structural index or guard |
| `model_schemas.py:15:48` | `'*.schema.json'` | artifact-bound: schema filename convention |
| `model_schemas.py:19:35` | `'$id'` | protocol-bound: JSON Schema; https://json-schema.org/draft/2020-12/json-schema-core#section-8.2.1 |
| `model_schemas.py:20:51` | `'.schema.json'` | artifact-bound: schema filename convention |
| `model_secrets.py:9:56` | `2` | safety-bound: structural index or guard |
| `model_secrets.py:9:76` | `0` | safety-bound: structural index or guard |
| `model_secrets.py:9:82` | `'.secrets'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:11:7` | `'\\'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:11:49` | `1` | safety-bound: structural index or guard |
| `model_secrets.py:11:56` | `'.'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:11:61` | `'..'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:14:26` | `'.secrets'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:15:29` | `'.secrets'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:17:46` | `1` | safety-bound: structural index or guard |
| `model_secrets.py:20:41` | `'utf-8'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:21:20` | `'\n'` | safety-bound: credential path or decoding restriction |
| `model_secrets.py:21:37` | `'\r'` | safety-bound: credential path or decoding restriction |
| `model_transport.py:14:30` | `'http'` | protocol-bound: URL scheme; https://www.rfc-editor.org/rfc/rfc9110#section-4.2 |
| `model_transport.py:14:38` | `'https'` | protocol-bound: URL scheme; https://www.rfc-editor.org/rfc/rfc9110#section-4.2 |
| `model_transport.py:16:51` | `'/'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:16:69` | `'.'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:16:74` | `'..'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:16:106` | `'/'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:17:15` | `'\\'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:17:35` | `'%'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:22:46` | `'localhost'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:23:24` | `'https'` | protocol-bound: URL scheme; https://www.rfc-editor.org/rfc/rfc9110#section-4.2 |
| `model_transport.py:25:72` | `'/'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:25:79` | `'/'` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:25:97` | `''` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:25:101` | `''` | safety-bound: URL joining or traversal restriction |
| `model_transport.py:34:20` | `'provider'` | configurable: backend setting or provider enum |
| `model_transport.py:34:35` | `'openai_compatible'` | configurable: backend setting or provider enum |
| `model_transport.py:36:24` | `'http'` | configurable: backend setting or provider enum |
| `model_transport.py:37:45` | `'connect_timeout_s'` | configurable: backend setting or provider enum |
| `model_transport.py:37:77` | `'read_timeout_s'` | configurable: backend setting or provider enum |
| `model_transport.py:38:43` | `'write_timeout_s'` | configurable: backend setting or provider enum |
| `model_transport.py:38:73` | `'pool_timeout_s'` | configurable: backend setting or provider enum |
| `model_transport.py:46:29` | `'http'` | configurable: backend setting or provider enum |
| `model_transport.py:47:41` | `'base_urls'` | configurable: backend setting or provider enum |
| `model_transport.py:47:54` | `0` | safety-bound: structural index or guard |
| `model_transport.py:49:38` | `'max_retries'` | configurable: backend setting or provider enum |
| `model_transport.py:49:55` | `1` | safety-bound: structural index or guard |
| `model_transport.py:51:33` | `'provider'` | configurable: backend setting or provider enum |
| `model_transport.py:51:48` | `'openai_compatible'` | configurable: backend setting or provider enum |
| `model_transport.py:60:39` | `'max_retries'` | configurable: backend setting or provider enum |
| `model_transport.py:63:56` | `'retry_statuses'` | configurable: backend setting or provider enum |
| `model_transport.py:63:93` | `'max_retries'` | configurable: backend setting or provider enum |
| `model_transport.py:65:45` | `'backoff_initial_s'` | configurable: backend setting or provider enum |
| `model_transport.py:65:69` | `2` | safety-bound: structural index or guard |
| `model_transport.py:65:89` | `'backoff_max_s'` | configurable: backend setting or provider enum |

## Handoff

- **Current status:** IMPLEMENTING, incomplete; see the latest parallel decode continuation above. The request-hash/engine metadata contract gap remains.
- **What was already done:** concurrent decode and process backstop, response validation, profile-to-effect/refusal table, per-occurrence literal classification, six killed mutants and a full suite with 1075 passed / 25 skipped.
- **What the next agent must do:** obtain PM direction for engine identity/version configuration and implement contract-complete request hashing/call records; verify the hash changes for dropped configured fields, then arrange required independent reviews. `vision.transport` belongs to T-008 under the PM decision pending user review.
- **What must not be repeated:** no live model, GPU, E12, or cloud call belongs in this patch; do not count the in-memory doubles as engine evidence.
- **Worktree, base and partial patch:** `.tmp/worktrees/T-009`, base `53535a3`, uncommitted worktree patch; no separate partial patch written.
- **Decisions and notes for the PM:** prior stage ownership remains a PM decision pending user review; no new product decision was made. Cleanup command was rejected by policy; listed scratch remains.
- **Lessons learned:** generic schema-valid fixture values can violate cross-field URL safety; use explicit safe URLs and relative routes when testing a loader override.
