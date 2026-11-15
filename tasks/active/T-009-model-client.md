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

(Codex writes Step 0 here and stops.)

## Steps

## Completion report

## Noticed, not done
