# KBS-F2: role settings in production capability probes

## Task brief

- **Task name:** KBS-F2 phase 1; approved implementation and continuation. R-015, R-018, R-020, R-026.
- **Responsible role:** assigned Codex implementer; no delegation, review or Git writes.
- **Task difficulty and assigned model:** nontrivial, not small; fresh Codex implementer, dispatched effort `high`; retain launch configuration. The exact model identifier is not independently verified here.
- **Reasoning effort and reason:** `high`, request construction, proof identity and safe diagnostic propagation cross several boundaries.
- **Preloaded skills:** [config-audit](../../.agents/skills/config-audit/SKILL.md), [handoff-report](../../.agents/skills/handoff-report/SKILL.md).
- **Current status:** REVIEWING; implementation and required serial verification complete. Await PM's Gemini/fresh Codex reviews. Q1-Q4 applied as approved; no profile retuning or new key.
- **What was already done:** reread the run evidence, request builders, consumers, configuration and relevant tests; inventoried bypasses below.
- **Expected output / Done check:** approved steps 1-5: failing-first regression before runtime edits, every listed/affected reader file green serially, about eight scratch-only mutants, executed row-by-row Configuration audit and this Completion report with the dev probe-send schedule. Stop for PM's Gemini/fresh Codex reviews.
- **Scope now:** approved model-client/probe/mapping modules, focused pipeline tests and synthetic fixtures, request/call-rule documentation, this task file and owned `.tmp/KBS-F2/` evidence. No stage runtime refactor or shared PM-file edit.
- **Out of scope:** live counterfactuals, GPU, installs, external network, model calls, EKS-2 resource gates, EKS-3 driver cleanup, embedding protocol changes, implementing live-look, full-suite execution, PM-owned files, Git writes. Run folder is read only; `database/` is read only; no access to credential storage or writes under `models/`.
- **May assume:** pinned synthetic challenge bundle and strict integer answer contract remain; preserve existing fallback policy, shared actual-send accounting, image preparation and labelled test seams. Existing provider mappings may be reused as project behavior, without claiming new provider support.
- **Must ask:** new key, expanded protocol mapping, changed fallback meaning, spend, model identity or deployment setting. Q1-Q4 were explicitly approved; the continuation requires a stop/report for a newly found runtime defect.
- **Run limit:** bounded CPU tests only, one pytest process/file at a time, no `-n`, no tool-suite mixing. One fixed `.tmp/KBS-F2/basetemp`, removed after every run. Child processes require `CREATE_NO_WINDOW` and faulthandler.
- **Step budget:** implementation proposal has five steps below; report repeated failures at the same step after two attempts. A later implementation dispatch must set its execution budget.
- **Base:** PM supplied main `0220123`; run source identity is `02201234a2689801622d7a20bb3ddf1a7d3aa5c5`. No Git state writes.
- **Ignored evidence paths:** `<project>/runs/E-kbsync-20261118-024404/{report.md,profile-common.yaml,profile-smoke.yaml,http/16628-0002/,http/16628-0003/}`. `<project>` means the main checkout, not this worktree. Implementation tests must use committed synthetic fixtures, never require these ignored files.

## Step 0

### Unknowns and failure modes

1. **Bootstrap/generation mismatch:** normal OpenAI-compatible generation also drops unproved reasoning and sampling (`model_mapping.py:18-40`, `model_client.py:696-708`). Merely calling that mapper in a probe still omits thinking-off. Conversely, granting every control a positive capability because a challenge succeeded would falsely prove semantic effects. Q1 addresses this explicitly.
2. **Cross-role proof reuse:** production proofs, attempted marks and multiple-image marks are backend-only (`model_client.py:285-292,374-379,436-446`). Roles in the captured profile use different token limits (256, 512, 1024, 1536, 768). Correct isolation increases possible sends; existing cap 8 must remain a cap, not become an implicit larger allowance.
3. **False negative classification:** an envelope with `length`, refusal, invalid JSON/shape, or a valid wrong integer currently becomes `False`; errors then lose their specific cause. Preserve strict checks and never put arbitrary response text, finish strings or exception messages in diagnostics.
4. **Effective-role drift:** resolved writer output, image/budget fallbacks, backend image caps and final `extra_body` precedence can change the request. Key and build against the effective backend and resolved role, not merely the role's declared backend or an earlier configuration snapshot.
5. **Test compatibility and environment:** many tests inspect `run.probes['main']`, `ProbeResult` booleans or the old eight-send schedule. Preserve explicit injection seams without allowing backend-only injected evidence to masquerade as production proof. No live result is inferred from doubles. `rg` and dedicated Read/Grep tools were unavailable; bounded PowerShell reads/searches were used. The Node read fallback was denied by the role allowlist and was not retried.

### Ranked questions with proposed answers

The following Step 0 proposals were all explicitly approved by the PM for implementation as **PM decisions pending user review**. The PM owns STATUS/decision recording; the worker does not edit those files.

1. **How do configured but semantically unproved controls reach both probe and generation?** Proposed: one low-level builder sends the existing, source-backed provider/engine mappings in the candidate probe request, and generation uses exactly the accepted effective settings for that proof context. Track request acceptance separately from semantic-control capability. A correct answer proves the challenge under those settings, not that thinking or a sampler obeyed its knob. Keep `reasoning.mode`/`sampling.*` semantic proofs unknown unless an independent observer proves them. Unknown/unsupported mappings remain dropped with warnings on both paths. Do not synthesize positive capability flags to make the mapper send fields. This changes the old drop-on-unknown behavior and needs explicit PM approval; a rejection requires a revised plan, not a silent workaround.
2. **Where does probe `max_tokens` come from?** Proposed: retain the requesting role's validated `max_tokens`; no bundle-owned cap and no new probe setting. This preserves request equivalence and reports truncation honestly. Do not increase 256 to conceal the captured omission.
3. **May roles reuse a proof?** Proposed: only if their canonical request-shaping contexts are identical, including token limits and resolved output/image policy. Role names alone need not prevent reuse; different contexts always do. All contexts still share one backend/run allowance, ledger, transport and lock. Exhaustion gives `PROBE_INCOMPLETE` without resetting a cap or borrowing another context's success.
4. **Which failure codes reach the operator?** Proposed: `PROBE_TRUNCATED`, `PROBE_REFUSED`, `PROBE_MALFORMED`, `PROBE_WRONG_ANSWER`; retain `PROBE_INCOMPLETE` for allowance/other unfinished cases, `PROBE_FAILED` for infrastructure failure, and existing budget/configuration codes. For several failed required capabilities, choose the first failed capability in stable demand order, with every case reason retained in safe evidence. No free-form model text is surfaced.

### Class sweep: all model request construction outside the role builder

Source references below are baseline lines at the supplied base. R = reasoning, S = sampling, T = token limit, O = output, I = image settings, X = backend extras. The real-call baseline is `model_client.py:664-727`: capability-gated R/S; role T; resolved O; role/vision/backend I; X merged last.

| Site / path | R and S applied today | T and O applied today | I and X applied today | Proposed disposition |
|---|---|---|---|---|
| `model_probe.py:141-180`, OpenAI-compatible text, image, JSON and image+JSON challenges | Neither R nor S | T from role; O selected by demanded challenge; JSON strictness from role, synthetic schema | I uses `prepare_probe_image:125-138` and caller's effective side; X last at :174 | Shared provider builder with effective role controls; only challenge content/schema/format differ deliberately. |
| `model_probe.py:175-179` -> `model_cloud.py:31-73`, cloud challenges | Existing cloud mapping of role R/S; unsupported controls warned by builder, but probe discards warnings | Role T; challenge O; current cloud builder refuses structured O | Prepared I; backend image permission checked; Gemini images refused; X last | Retain shared cloud construction and its refusals; preserve warnings in probe evidence. No new cloud format/image support. |
| `model_probe.py:307-332` -> `model_client.py:303-317`, lazy ordered multi-image challenge | Neither R nor S | Caller passes role T; O absent, even for structured roles | I already uses effective side, role encoding/quality, alpha and decode limit; X absent | Route both orderings through shared builder and resolved output policy, including strict synthetic schema and X. |
| `model_client.py:345-431,620-625`, startup and lazy demand/combination extension | Delegates to challenge construction above | Uses passed resolved role in lazy call; startup uses configured role | Effective backend, with image prohibition handling | One context-specific construction and lookup at every entry; no backend-only gate after role resolution. |
| `model_client.py:502-548`, injected ordinary/image/thinking/multiple-image plans; `model_probe.py:243-304,335-370` | Arbitrary caller-supplied bodies; thinking pair intentionally varies one switch | Caller supplies T/O or omits them | Caller supplies I/X; pair validators check controlled differences | Explicit labelled-test/advanced injection seam, not production factory. Preserve pair validation and compatibility. Production callers never supply handcrafted plans; any production use must build bodies through shared construction. |
| `model_probe.py:53-65,226-240` -> `model_transport.py:50-85`, probe sends and retries | No mapping or mutation | Same body on every retry | Same body/headers, per-send allowance debit | Keep exact body on retries; do not rebuild with different defaults. Context partitioning must not partition the budget. |
| `model_client.py:602-608,626-637,758-770`, image/budget fallback | Real builder uses requesting role with effective backend; current proof may be backend-only | Role T and resolved O | Effective backend image cap and X | Recompute proof context and request on each fallback. `_multiple_images_pair` currently requires declared backend equality (:309); remove that production gap via effective-role construction, not borrowed proof. |
| `k2_naming.py:548`, `k2_observe.py:284`, `k3_cards.py:295`, `k4_aliases.py:194`, `planner.py:306-325,371`, `profiles.py:303` | Delegate to bound role client | Same | Same | Keep delegation. Planner retries call same role path; verify with consumer tests. No separate HTTP builder found here. |
| Live-look / on-demand observation | No active request path found | None | None | Source-verified refusal at `config.py:1697-1700`; `model_client.py:89-93` only defines permitted image-role names. Do not invent or implement a live-look path. Future implementation must use bound role client. |
| `k1_embed.py:263-295`, embedding image/text bodies and `_post` | R/S not applicable to embedding protocol | No generation T/O | Embedding-specific instruction/template, resize/encoding, dimensions and `embedding.request.extra_body` | Structural exception by protocol: this is an embedding builder, not a `vlm.roles` chat request. No chat capability proof used; do not add generation fields or change embeddings. |

Search covered every `src/prompt_enhancer/**/*.py` occurrence of `post`, `post_probe`, `cloud_request`, `map_generation_fields`, `production_probe_cases`, `multiple_images_plan`, `call` and live/observer references. `embed_server/server.py:361` is a receiving HTTP handler, not an outbound model request.

### Proposed design

**Shared construction.** Extract the existing local role body assembly into a pure low-level builder in an existing mapping module. Inputs: effective backend, resolved role, system/user content, explicit output schema, prepared images and explicit evidence/mapping policy. It must not call the public client, send HTTP, load credentials or trigger probes. Local production challenges, ordered pairs and real calls use it; cloud challenges/calls continue through `cloud_request`. Use the same provider field spelling, `none` handling, configured template key and merge-last X everywhere. Prepared images retain the shared `prepare_image` path and decision 144's side resolution. Resolve role output before context selection; text challenges intentionally use no constrained format, format challenges use their demanded format and pinned synthetic schema rather than the task's semantic schema.

**Bootstrap policy (Q1 pending).** Separate three facts: a field can be mapped by verified provider/engine construction; the configured request was accepted and answered correctly; the semantic effect of a control was independently observed. Candidate requests may include the first kind without asserting the third. Persist effective field identity and warnings. Real requests retain the same mapped controls as the accepted proof; known unsupported fields are dropped and warned consistently on both paths. A failed request does not silently retry with thinking/sampling removed. Retain explicit injected semantic proofs where tests supply them, and do not turn ordinary challenge success into `reasoning.mode: true`. No new mapping for repetition penalty or another unknown field is authorized implicitly by this plan.

**Proof identity.** Use a run-owned production cache indexed by `(effective_backend_name, canonical_context_digest)`; each context contains capability results and independent attempted/multiple-image state. Include provider, resolved engine name/version, model identity/revision, chat endpoint identity, final backend X, role `max_tokens`, all configured R/S (even dropped values), resolved O including referenced schema identity/hash, image policy and resolved vision values, effective side/backend side, decode limit, and bundle/challenge-builder identity. Record exact challenge body hashes and prepared-image hashes as evidence. Demand-specific text/format/image/combination/ordered proofs stay separate inside the context. If mapping policy changes the effective shaping, it changes identity. Exclude credentials and admission-only knobs from the digest.

No production gate may consult `self.probes[backend_name]` as authoritative for another context. Cover `for_role`, `ensure_role_capabilities`, `_start_backend`, lazy resolved-writer proof, multiple-image extension, fallback and call-record evidence. Retain backend-only injected maps as an explicit nonproduction seam, not a cache fallback. The context digest and canonicalization are artifact-bound protocol identity, not a user setting. Existing offline `semantic_role_settings` is related evidence, not automatically reusable: it omits decode bounds and has checkpoint-compatibility behavior. Avoid changing its meaning unless required, with its readers tested.

All contexts share `_probe_allowances[backend]`, transport retries, backend lock, concurrency and cloud ledger. Failed/incomplete attempts are remembered within their own context; escaping cancellation/unsent refusal retains KBS-F1's retryability and already spent sends. Do not retain a role's context in one mutable global/current slot across awaits. A different role or writer manifest can exhaust the existing allowance; report that honestly. The old eight-send production test assumes backend-wide reuse and must be revised to exercise equal-context reuse and unequal-context refusal without silently retuning deployment profiles.

### Typed outcome and propagation

Add an internal immutable verdict carrying success plus a closed reason code (and optional allowlisted finish category); keep existing boolean test seams compatible through an explicit adapter/wrapper. Production verification must retain typed reasons, including paired challenges, rather than convert the verdict to `bool` and lose it. Keep capability booleans and diagnostic cause separate; never create positive evidence from HTTP 200 alone.

| Structural observation | Typed code / behavior |
|---|---|
| OpenAI `length`, Anthropic `max_tokens`, Gemini `MAX_TOKENS` | `PROBE_TRUNCATED`; inspect finish metadata before trying to parse empty content. |
| Explicit provider refusal or documented safety/content-filter envelope | `PROBE_REFUSED`; classify from protocol structure, never prose keywords. Validate exact existing provider spellings before coding. |
| Invalid response JSON/envelope, missing/wrong-type content, non-JSON answer, wrong keys or non-strict-integer `value` | `PROBE_MALFORMED`; `true` and `7.0` do not count as integers. |
| Completed, well-formed `{"value": <integer>}` with a different integer | `PROBE_WRONG_ANSWER`; do not include the returned value/text. |
| Other structurally valid unfinished/unknown stop category; exhausted request allowance | `PROBE_INCOMPLETE`; never infer unsupported text from unfinished output. |
| HTTP/transport/timeout failure | Existing `PROBE_FAILED` with closed structural provenance; preserve distinct budget/config/artifact failures. |

Surface each case reason in `ProbeResult.errors`/evidence and safe challenge events. Extend `MODEL_CODES` and `safe_probe_events` (`model_client.py:29-64`) with closed allowed reasons, retaining sanitized role/capability/status/sent fields. Do not expose raw usage dictionaries, reasoning, refusal text or arbitrary provider finish strings. Required-capability errors select the typed failure (stable demand/case order) rather than replacing it with `CAPABILITY_UNAVAILABLE`. Existing `ModelCallError` attachments must work on startup, lazy extensions and fallback.

`k2_naming.record_probe_failure:66-76` already writes the safe events and top-level error into `model_probe_failure`; K2 observation/K3/K4 share it. `failure_code:52-63` already trusts only `safe_model_code`. `kb_sync.require_success:29-36` counts allowlisted `MODEL_CODES`, so the same typed error must appear as, for example, `STAGE_FAILED: count=1; codes=PROBE_TRUNCATED=1`. Test all these readers; change them only if the shared code/event extension is insufficient. `profiles.py:303` is another client-error consumer to verify; planner intentionally returns its own stage code and must not be silently redesigned.

### Token-budget decision and evidence

Retain `vlm.roles.<requesting_role>.max_tokens`. `production_probe_cases:167` and `_multiple_images_pair:316` already use it; cloud mapping uses `map_generation_fields:25-26`. The bundle at `contracts/probes/model_capabilities.v0.json:1` contains challenge content, answers and image pins, with no deployment token budget. The captured text request actually had `max_tokens: 256`, matching `profile-common.yaml:47`; its failure followed omission of all R/S fields, not proof that 256 can never suffice. Captured schema reply stopped successfully under the same cap. No live counterfactual with thinking-off has been run. A new cap would enlarge configuration and would prove a different request shape; a hidden bundle cap would violate R-015. Q2 remains a proposal until approval.

### Research required before code

- Project-owned refactor: reread every changed symbol's callers/readers immediately before editing; retain the class table as the baseline inventory.
- Existing local reasoning/sampling mappings and precedence come from raw project code and `contracts/vlm-request.md`; do not claim new external support. Before enabling a previously gated mapping, inspect its existing T-009 pinned provider evidence or installed/local engine source. Missing primary evidence is a question/blocker for that mapping, not permission to infer it. No external fetch is authorized here.
- Provider finish/refusal classification: use captured OpenAI-compatible envelope plus existing provider adapter/tests; check raw pinned protocol evidence for Anthropic/Gemini classifications before extending them. No semantic refusal detection from natural language.
- Test harness behavior: inspect `test_slot.py --help` and the installed pytest/Hypothesis interfaces before executing the approved checks. Existing `loopback_server` is a labelled double, not a live model.

### Research evidence

| Question | Source / version | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| Is the root cause visible in actual requests? | Main run `http/16628-0002/{request,response}.json`, source identity `0220123` | Raw parsed, structural fields only printed | Request fields `model,max_tokens,messages`; cap 256; finish `length`; content length 0; reasoning length 897; completion tokens 256. | experiment (reread captured GPU-run evidence; no new run) |
| Does the schema comparison complete? | Same run `http/16628-0003/{request,response}.json` | Raw parsed | Adds only `response_format`; cap 256; finish `stop`; content length 11; reasoning length 242; completion tokens 60. | experiment (captured evidence) |
| What settings were configured? | Same run `profile-common.yaml:47-67,80`; raw base/dev profiles and deployment overlay | Raw | Role R is off; explicit S; several different role token limits; main cap 8. Deployment overlay sets X thinking switch, which can mask missing role mapping. | source-verified |
| What builds probes and generation? | `model_probe.py:99-180,243-370`; `model_client.py:303-551,578-818`; `model_mapping.py:18-69`; `model_cloud.py:31-73`, supplied base | Raw | OpenAI ordinary/ordered probes bypass R/S; real calls gate controls; cloud already shares a builder with restrictions. | source-verified |
| Are cache and diagnostics safe for different roles? | `model_client.py:29-64,374-431,436-474,654-661`; `k2_naming.py:52-76`; `kb_sync.py:29-36` | Raw | Cache/attempt keys backend-only; boolean verdict loses cause; centralized safe-code readers can propagate a new closed code. | source-verified |
| Are retry budgets shared actual sends? | `model_probe.py:38-65`; `model_transport.py:50-85`; KBS-F1 task lines 7-19,117-130 | Raw | Debit each retry; preserve cloud reservation and cancellation behavior; no resetting budget on a new proof context. | source-verified |
| Is live-look another bypass? | `config.py:1697-1700`; recursive outbound-call search; `planner.py:306-325,371` | Raw | Live reading is refused at this base; planner/retry already uses role client. Embedding calls have a separate protocol. | source-verified |
| Why role token cap and no new key? | Bundle line 1; role schema; `model_mapping.py:25-26`; `model_probe.py:167`; `contracts/vlm-request.md` rules 1-3; VLM rules 2-5 | Raw | Existing validated role cap is the request-shaping choice; bundle has no cap. | source-verified / documented |
| Existing tests encode the old mismatch? | `test_model_production_probes.py:159-179,419-445`; `test_kb_sync_production_probes.py:107-161` | Raw | Tests expect optional controls dropped, boolean verdicts and 8 sends; update assertions for approved semantics, not special-case fixtures. | source-verified |
| Any settled decision answers the new policy? | `tasks/STATUS.md`, `tasks/DECISIONS_INDEX.md`, lesson digest; recall searches below | Raw plus local indexed search | KBS-F1 requires settings-aware construction without fabricated optional semantic proof; no accepted resolution of Q1 found. | source-verified |

External primary provider pages were not reread (network forbidden); no new external API behavior is asserted. Unknown mapping/finish conventions remain required evidence before code.

### Configuration impact

The runtime change uses existing validated settings; no new profile key or profile-value retuning. The following inventory was filled before the first runtime edit. Schema owners: `contracts/schemas/vlm_role.v0.schema.json` and `enhancer_config.v0.schema.json`; runtime validators in `config.py` (`load_config`, role/backend validation and command read gates).

| Operational value / behavior | Existing validated paths and profile source | Proposed impact and checks | Literal class |
|---|---|---|---|
| Token limits | `vlm.roles.*.max_tokens`; role schema; base example :269 onward; dev inherits roles | Probe/real-call equality; varied caps partition proof context; missing, sentinel, boolean, zero/negative refused by existing load/read gates. | configurable |
| Reasoning and template key | `vlm.roles.*.reasoning.{mode,effort,budget_tokens,template_kwargs_key}`; base :280-284 and role schema; dev inheritance | Off/on/auto and effort/budget `none` semantics, non-default switch-key override; refusal of invalid/missing values and provider budget contradictions. Approved Q1 controls mapping, not a hidden default. | configurable |
| All sampling fields | `vlm.roles.*.sampling.*`; role schema, finite checks `config.py:766-770`; base :270-279; dev inheritance | Differential non-default override for every mapped field; unsupported mappings have identical omission/warnings; malformed, nonfinite, missing and sentinel refusal. Do not add engine mappings without source evidence. | configurable |
| Output and writer resolution | `vlm.roles.*.output.*`, writer manifest schema; existing base/dev profiles | Preserve strict/validate_response and transport policy; bind resolved format/schema hash into proof key; synthetic challenge schema/answers are explicitly different content. Existing unsupported cloud combinations remain refused. | configurable; challenge schema/answers artifact-bound |
| Model/provider/engine/endpoint/extras/fallback | `vlm.backends.*.{provider,engine,model,base_urls,endpoints,extra_body,allow_images,on_budget_exhausted,call_budget}`, `vlm.default_backend`; backend schema :3200 onward; both examples/deployment overlay | Effective-backend key and X-last equality, collisions tested and hashed; fallback probes its own context; no grant or identity change. | configurable; credential exclusion safety-bound |
| Image preparation and permissions | `vlm.roles.*.image.*`, `vision.{max_image_side,format,alpha_policy}`, `vlm.backends.*.image.{max_side,max_decode_pixels}`; role/backend schemas and examples | Preserve decision 144 and existing preparation; vary side/encoding/quality/decode policy; no-upscale and prohibition checks; prepared bytes enter body/evidence identity. | configurable |
| Probe/HTTP limits, queues, ownership | `vlm.backends.*.probe.{max_requests,timeout_s}`, `.http.*`, `.max_concurrency`, `.call_budget`; schema :3541-3557; base :200, dev :68 | Unchanged cap and retry/timeout semantics across all contexts; serial/concurrent role isolation tests; cancellation clears only unobserved attempt marks. Stop owned test server threads/processes in finally. | configurable; accounting/lifecycle safety-bound |
| Proof identity and typed diagnostics | No deployment setting; closed protocol codes, context digest and existing checkpoint artifact | Override/refusal tests via their contributing settings; mutation/property checks reject unknown diagnostic inputs; sanitized reasons only. | artifact-bound / protocol-bound / safety-bound |
| Paths/device/GPU/install/trace storage | Existing project containment, run-owned artifact writer; no new path/device setting | No change; source run read only; no GPU/model work; default scratch only below worktree. No standalone trace store. | unchanged; containment safety-bound |

Both examples already declare/inherit the existing fields; no value retuning is proposed. If implementation introduces any new user key, stop for scope/design review and add validation, both examples, override and refusal tests before using it.

### Plan with checkable finish (original Step 0; completed execution recorded below)

1. **Pending approval: failing-first regression.** Add committed, labelled synthetic request/response envelopes reproducing the two captured probe shapes and the role's off/sampling/256 settings. Use `loopback_server` with the production composition/CLI path, no probe injection: absent/mismatched controls produce the captured structural `length`/empty-content result, a conforming request receives a synthetic correct answer. Assert probe and real request fields against the approved role builder, unchanged 256 cap, typed truncation artifact/summary and no assessment call on the failing branch. Run before runtime edits and record the actual expected failure. Separate fixed truncated-response test must remain truncated after the construction fix. This is a loopback double/counterfactual contract, not evidence the real model would succeed.
2. **Pending: shared builder and context cache.** Implement approved Q1, ordinary/paired/lazy/fallback construction and context-specific gates/attempt markers while retaining one backend allowance. Verify two equal-context roles reuse, every independently varied setting separates both positive and negative proof, resolved writer schemas separate, ordered-pair image policy/X match, and fallback uses its own effective backend.
3. **Pending: typed verdict and reader propagation.** Add closed outcomes and safe event reasons through ordinary and paired proofs, required-capability error, durable `model_probe_failure`, code/count `STAGE_FAILED`, and successful call evidence. Property-test malformed provider envelopes, arbitrary hostile diagnostic strings, refusal/truncation precedence and wrong answer types; no natural-language parsing.
4. **Pending: bounded focused verification and mutations.** Run each required file serially through `test_slot.py` as described below; run generated differential R/S/T properties and loaded configuration overrides/refusals. Mutate 3-10 changed lines on copies of only changed modules under task scratch: omit R, omit S, change T source, ignore context in lookup, omit output/image/X from key, collapse typed reason, bypass sanitizer, reset allowance. Target about 8 meaningful mutants; report killed/surviving/not_run. Fix findings without changing accepted product meaning.
5. **Pending: audit and handoff.** Fill every configuration-audit row with actual test names/results; run applicable lint and `ruff format --check` if installed, report exact output or unavailable/not_run; remove own scratch after validating resolved paths and removing links as links. Check only scoped diffs, report unrun checks honestly, stop for Gemini and fresh Codex review by the PM. No worker self-review or Git write.

### Test sets and execution contract (not_run in Step 0)

Required files, one per process: `tests/test_model_production_probes.py`, `test_kb_sync_production_probes.py`, `test_k2_probe.py`, all `test_k2_fix_round*.py` (rounds 1, 2, 3 at this base), all `test_model_client*.py`, `test_model_transport.py`, `test_pipeline_s0_s3.py`, `test_field_contracts.py`, `test_storage_anchored_writes.py`, `test_property_settings.py`.

Reader search additionally identified: `test_model_probe.py`, `test_model_mapping.py`, `test_model_cloud.py`, `test_model_composition.py`, `test_model_config.py`, `test_k2_semantic_keys.py`, `test_k2_config.py`, `test_k2_observe.py`, `test_k2_observe_config.py`, `test_k3_cards.py`, `test_k3_config.py`, `test_k4_aliases.py`, `test_k4_config.py`, `test_kb_sync_gates.py`, `test_storage_tables.py`, plus fixture consumers of `k2_fixtures.py`, `k2_observe_fixtures.py`, `k3_fixtures.py`, `k4_fixtures.py`, `planner_fixtures.py`. Before runtime completion, repeat the symbol/structure reader search against the actual diff and run every affected reader test, including `test_kb_sync_diagnostics.py`, `test_planner.py` and `test_profiles.py` where their error/record path is affected. If a shared fixture changes, expand to all its consumers; do not treat this list as a full-suite request.

Use project `.venv` interpreter `<project>/.venv/Scripts/python.exe`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`. Check limiter help first. Command pattern, run from worktree root with an explicit test-file argument each time:

```text
<venv-python> -B tools/agent-workflow/test_slot.py -- <venv-python> -B -X faulthandler -m pytest -q tests/<one-file>.py --basetemp=.tmp/KBS-F2/basetemp
```

Use the suite's configured bounds/timeouts; inspect before selecting additional flags. Bound every subprocess and hide its window. Delete the fixed basetemp after each run, preserving small cited logs outside it. No full suite, network outside the labelled loopback fixture, GPU, inference, downloads or installs. No test is counted as passed until its real output is recorded.

## Steps

- **Final steps 1-4 complete:** red-before-runtime regression retained; shared construction/context proofs, typed failures and final image-byte digests implemented. Final serial verification: 37 files, 3126 passed, 41 existing skips; all eight scratch-only mutants killed, no survivors or invalid mutants. Exact commands and outputs in `.tmp/KBS-F2/verification.md`, `commands.json` and `mutations.json`.
- **Final step 5 complete:** Configuration audit below records executed override/refusal checks per affected row; runtime literals classified. Ruff is unavailable and honestly not_run; no installation. Cleanup leaves only small evidence reports and no owned test processes, mutable module copies or basetemp.

- **Continuation, step 4 in progress:** replace every provider-switching backend in the changed/new tests with one example-derived, loader-validated fixture helper; fix the bound-client assertion. Repeat reader searches and run each required file serially.
- **Continuation, step 5 pending:** prepared-image SHA-256 provenance is artifact-bound, computed from final request bytes after existing configured preparation/X merge; no new operational setting. Complete about eight scratch mutants, executed configuration audit and cleanup before PM review.
- **Continuation test defects resolved:** `synthetic_provider_backend` copies the appropriate example backend, fills all synthetic provider-dependent fields, and validates the whole candidate through `load_config` before use. Context provider switch, retry transport fixture, cloud zero-grant fixture and `cloud_profile` all use it; the updated bound-client assertion uses `client.run`. No backend-switch fixture exists in `test_kb_sync_production_probes.py`; its endpoint override remains an explicit loopback test override.
- **Continuation verification in progress:** initial reruns `101 passed in 12.61s` and `84 passed in 16.63s`; production CLI `1 passed in 4.20s` with exactly 20 sends. Serial reader queue still running; final new-file rerun, mutations and final audit remain pending. Evidence labels `resume-*` and `focused-*` in task scratch.

- **Phase 1, step 1 complete:** before runtime edits, serial loopback regression produced `2 failed in 2.20s`; missing controls blocked generation and truncation collapsed to `CAPABILITY_UNAVAILABLE` instead of `PROBE_TRUNCATED`. Exact output: `.tmp/KBS-F2/red.log`; command in `.tmp/KBS-F2/check-results.md`.
- **Phase 1, steps 2-3 implemented, unaccepted:** shared construction, context-specific production proofs and closed typed verdict propagation; two-case loopback regression subsequently returned `2 passed in 1.83s`. Re-read existing T-009 mapping evidence at `<project>/.tmp/T-009/provider-docs.md`; no new provider mapping or profile value.
- **Phase 1, step 4 stopped:** the new property file's provider-switch fixture failed twice, first with provider/engine mismatch and then missing `anthropic_version`; latest file result `1 failed, 99 passed in 15.70s`. The existing production file's updated assertion also has `NameError: name 'run' is not defined`, latest result `1 failed, 83 passed in 12.94s`.
- **Phase 1, step 5 partial:** exact outputs and incomplete configuration audit below; no acceptance claim, no mutants or remaining file runs. All owned test processes exited; fixed basetemp removed by the runner. Stop for PM redispatch under the explicit two-failure rule.

- **Step 0 research complete:** raw evidence and source inventory establish omission, backend-only cache and collapsed diagnostics; recall and reader searches completed.
- **Step 0 deliverable complete:** design/options, impact inventory and five-step acceptance plan recorded; runtime edits and tests remain not_run pending approval.

## Step 0 completion report (historical)

- **Work completed:** created this Step 0 task file only. No runtime/configuration/test edit, model request, GPU job, install, delegation or Git write.
- **Result and evidence:** raw captured envelopes were parsed locally: `16628-0002` fields `model,max_tokens,messages`, cap 256, finish `length`, content length 0, reasoning length 897, completion tokens 256; `16628-0003` adds `response_format`, cap 256, finish `stop`, content length 11, reasoning length 242, completion tokens 60. No model text was printed in that extraction. Inventory, design and research tables above are the checkable Step 0 output.
- **Actual commands / output:** `<venv-python> -B -I tools/agent-workflow/recall.py --help`, `build --help`, `search --help` exited 0; `build --db .tmp/KBS-F2/recall.sqlite` -> `indexed=94 unchanged=0 removed=0 refused=0 chunks=689 skipped_chunks=0`. Searches `probe reasoning sampling` returned no hits; `probe` returned T-006-race followup, decision 150 and STATUS; `reasoning` returned N117, N074 and N055. Query searches used `--limit 3` (combined query `--limit 4`) and the same task-local database.
- **Important changes and reasons:** Q1 isolates the existing control-proof circularity rather than fabricating optional capability evidence; Q2 retains the role cap; Q3 replaces backend-only proof identity without increasing any send limit. All remain proposals.
- **Checks (static documentation):** `<venv-python> -B -I .tmp/KBS-F2/check_step0.py` exited 0: `STEP0_CHECK: PASS; required sections/codes present; no trailing whitespace; runtime tests not_run`. The checker inspected required sections/codes and trailing whitespace. Scoped `git status --short --` for the task document, four model modules and two example profiles printed only `?? tasks/active/KBS-F2-probe-request-settings.md`. Pipeline tests, regression, properties, configuration tests, mutants, lint and `ruff format --check` are **not_run**, plan-only dispatch. No claim of live counterfactual success.
- **Cleanup:** removed only task-owned `recall.sqlite` and `check_step0.py`, then the empty `.tmp/KBS-F2` directory, without recursive deletion. `Test-Path` verification printed `CLEANUP: PASS; task scratch absent`; final scoped status again listed only the new task document. No process/server was started.
- **Problems, risks and remaining work:** Q1-Q4 require PM review before runtime work; no external provider primary-page recheck authorized; correct context isolation can consume more of the unchanged backend allowance. Tool limitations are recorded above. EKS-2/EKS-3 remain outside scope.

### Step 0 configuration audit (historical)

No runtime change in this phase. Impact inventory above is complete for the proposed phase; implementation audit/tests are **not_run** and cannot count as accepted runtime work.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Step 0 documentation only | No schema/profile change | Plan contents checked; no runtime edits authorized | Existing profiles read only | not_run: implementation awaits approval | No new runtime literal |

### Noticed, not done

EKS-2 resource-floor and EKS-3 runner-teardown findings belong to separate tasks; no driver or resource-setting changes proposed here.

## Step 0 handoff (historical)

- **Current status:** STEP_0_COMPLETE, awaiting PM plan approval; STOP.
- **What was already done:** raw run request/reply facts reread, class sweep and reader inventory completed, proposals and acceptance checks written here.
- **What the next agent must do:** PM review Q1-Q4 and approve or amend the plan; send a separate implementation dispatch. Implementer then runs the failing-first loopback regression before runtime edits and completes all five steps.
- **What must not be repeated:** no live retry, silent token/cap increase, treating semantic-control support as proven from a correct integer, backend-only production cache reuse, external calls, full suite or Git writes.
- **Worktree, base and partial patch:** `.tmp/worktrees/KBS-F2`, supplied `0220123`; task document only, no partial runtime patch.
- **Decisions and notes for PM to merge:** Q1-Q4 are PM decision pending user review; PM owns STATUS/decision recording. Reviews by Gemini and a fresh Codex reviewer are dispatched by PM after implementation.
- **Lessons learned:** no implementation lesson yet. Dedicated read tools and `rg` are unavailable in this worker; use permitted bounded reads rather than retrying a denied tool. The static checker was moved into task scratch after the inline-code size guard rejected its command; run multiline/long logic from scripts. The checker and task-local recall database are disposable and removed before handoff; its exact output is preserved above.

## Completion report

- **Work completed (R-015/R-018/R-020/R-026):** `model_mapping.py:43` supplies one pure OpenAI-compatible request builder for production probes and real calls. `model_probe.py:118` retains immutable typed verdicts while preserving the boolean test adapter; `:202` hashes final prepared image payloads. `model_client.py:304,378,487` isolates ordinary/lazy/ordered/fallback proofs by resolved request context, with one backend allowance, transport, ledger and lock. Startup waits for writer manifest resolution; call gates/evidence use the returned context, not the backend inspection view. Contracts and call rules document acceptance versus semantic-control proof. No profile value, schema or user setting was added/retuned.
- **Changed files:** three runtime modules above; `tests/test_probe_request_settings.py`, `test_model_production_probes.py`, `test_kb_sync_production_probes.py`, `test_k3_cards.py`, `test_k4_aliases.py`; `contracts/vlm-request.md`, `docs/rules/VLM_CALL_RULES.md`; this task file. `model_cloud.py` and both example profiles are unchanged.
- **Done-check result:** **PASS**, ready for delegated review. The captured-shape loopback regression was red before runtime edits (`2 failed in 2.20s`, retained `red.log`). Final serial runs: **37 files, 3126 passed, 41 skipped**, all file processes exit 0. Final request-setting file: `105 passed in 11.60s`; production probes: `84 passed in 16.63s`; production CLI: `1 passed in 4.20s`; S0-S3: `1 passed in 3.46s`. Classification: CPU unit/mock_contract, labelled doubles only. Exact argv/environment and each real output summary: [verification](../../.tmp/KBS-F2/verification.md), [commands](../../.tmp/KBS-F2/commands.json). Totals count each final file once.
- **Skip accounting:** 34 field-contract cases have no supported alternative/invalid schema candidate (`test_field_contracts.py:171,215,238,250`); 2 profile and 5 anchored-write cases are POSIX-only (`test_profiles.py:635`, `test_storage_anchored_writes.py:39`). No skip was added. Linux remains not_run here; these branches belong to CI.
- **Mutations:** **8/8 killed**, no survivor or invalid mutant. Only the changed module was copied; other source was read in place via a package-path shim. Mutants omitted thinking, omitted sampling, changed token limit, removed inference context, removed decode context, collapsed truncation, corrupted prepared-image digest, and reset allowance. Every mutant ran this one test file through `test_slot.py`; failures were assertions, not import/collection errors. Exact operators, summaries and failing-test names: `mutations.json`, `verification.md`, `commands.json` in task scratch. Module copies were removed after each run; the working source was never mutated.
- **Test-defect class sweep:** `synthetic_provider_backend` in `test_model_production_probes.py:52` derives and loader-validates a complete example backend (provider/engine/route/provider keys). It is used by the context provider switch, retry transport fixture, zero-grant cloud fixture and `cloud_profile` (which supplies all other cloud cases). No provider-switch fixture exists in `test_kb_sync_production_probes.py`; its route changes are explicit loopback overrides. The changed bound-client assertion uses `client.run`. K3 and K4's malformed-boolean probe cases now expect the approved `PROBE_MALFORMED`, preserving their negative proof status; all matching stale expectations were searched. These were test fixes, not runtime workarounds. No new runtime defect was found in the continuation.
- **Request class-sweep dispositions:** ordinary/ordered probes and production real calls share `openai_request`; cloud calls continue through `cloud_request` with warnings retained. Lazy proofs, resolved writers and image/budget fallback use the effective context; retries resend the same body and debit the same backend allowance. Explicit injected pairs retain their labelled seam and strict controlled-difference validation. Stage/planner/profile callers remain bound-client callers. Live-look is still structurally refused; embedding requests remain a separate unchanged protocol. Final prepared-image digests include resize/encoding and final engine-extra overrides; ordinary and ordered proof evidence retain them alongside exact body hashes. Reader tests cover durable `model_probe_failure` and `STAGE_FAILED` code/count propagation without model text.
- **Dev send count:** full demand of the five distinct dev role contexts, no retries: assessment 2 + ordered folder naming 8 + observation 6 + cards 2 + aliases 2 = **20 sends**. Single-image naming needs 18; no naming needs 12. The production CLI double verified the 20-send schedule. The existing run allowance 8 is unchanged; PM selects the next smoke value and accounts for any retries. This is schedule arithmetic plus a CPU contract test, not a live dev run.
- **Static checks:** scoped `git diff --check` over all tracked changed files exited 0 with empty output. Actual `<venv-python> -B -I -m ruff check <changed Python files>` and `-m ruff format --check <changed Python files>` each returned `<project>\.venv\Scripts\python.exe: No module named ruff`; lint/format are **not_run**, tool unavailable, no install. The runtime diff's new literals are classified below; no new operational default or threshold.
- **Research/recall:** existing project/provider mapping evidence reused; no external provider behavior added. Continuation recall build: `indexed=94 unchanged=0 removed=0 refused=0 chunks=689 skipped_chunks=0`; search `probe provider fixture` returned no hits. Final prepared-image parser is source-verified in `model_probe.py:202`, exercised by independent sent-byte comparisons and generated hostile payloads. Dedicated read/Grep tools and `rg` were unavailable; a clock-wait attempt was denied by the role allowlist, so bounded process-output waits were used.
- **not_run / limits:** full suite (PM-owned), Linux/CI, live model/GPU/cloud calls, live counterfactual and deployment performance. No external network, installs, model downloads, Git writes or credential reads. Request acceptance under controls does not establish those controls' semantic effect; optional semantic capability flags remain unknown unless independently observed.
- **Problems/remaining work:** no known open implementation/test defect. PM's Gemini and fresh Codex reviews, integration and full suite remain. Ruff availability and the authorized live counterfactual belong to later environment/run work. Cleanup verification is recorded in Worker completion.

### Configuration audit

The phase 3 test results and exact command forms are under [Fix round 1](#fix-round-1). Schema owners are `vlm_role.v0` and `enhancer_config.v0`. Base and dev examples and the deployment overlay supply the existing values. The old execution counts remain in the historical completion report above.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test; actual file result | Retained-literal class |
|---|---|---|---|---|---|
| Role token limit | `vlm.roles.*.max_tokens` | Strict role schema and command read gate | Base role fields, inherited by dev | Loader refusal in `test_control_configuration_refusal`. Context in `test_each_request_setting_partitions_context`. Wire cap in `test_loaded_nondefault_control_override_in_probe` and `test_captured_run_shapes_with_role_controls_over_loopback`. | configurable |
| Reasoning mode/template key/budget | `vlm.roles.*.reasoning.{mode,template_kwargs_key,budget_tokens}` | Types, mode enum and Anthropic budget relation | Both examples | Loader refusals in `test_control_configuration_refusal`. Context in `test_each_request_setting_partitions_context`. Wire switch and local budget warning in `test_loaded_nondefault_control_override_in_probe`. Injected wire behavior in `test_loaded_profile_composes_role_client`. | configurable, field names protocol-bound |
| Reasoning effort | `vlm.roles.*.reasoning.effort`, `vlm.backends.*.engine` | Role enum and exact documented local engine/version support | Both examples | Loader acceptance and ordinary, ordered and generation wire/warning assertions in `test_loaded_reasoning_effort_uses_documented_local_enum`. Invalid types or missing/sentinel effort in `test_control_configuration_refusal`. Engine refusal in `test_engine_profile_override_and_refusal`. Context in `test_each_request_setting_partitions_context`. | configurable, enum/version mapping and warning protocol-bound |
| Every sampling field, including repetition penalty | `vlm.roles.*.sampling.*` | Type, range and finite checks | Both examples | Loader refusal in `test_control_configuration_refusal` and `test_nonfinite_sampling_refused`. Context in `test_each_request_setting_partitions_context`. Wire values or explicit omission warning in `test_loaded_nondefault_control_override_in_probe`, real-call counterparts in `test_loaded_profile_composes_role_client`. | configurable, wire names protocol-bound |
| Format, schema resolution, strictness and validation | `vlm.roles.*.output.{format,schema_id,strict,validate_response}`, admin manifest schema | Schema lookup/hash, manifest resolution and response validation | Both examples | Loader round trip/refusal in `test_role_values_through_command_load`. Context in `test_resolved_writer_manifests_have_separate_proofs`. Wire format/schema/strictness and local validation in `test_loaded_profile_composes_role_client`, structured validation refusal in `test_structured_output_without_validation_is_refused`. | configurable, synthetic schema artifact-bound |
| `output.transport` | `vlm.roles.*.output.transport` | Schema accepts `engine_native` for the manifest-drafter round trip, naming command requires `response_format` | Both examples | Loader acceptance/refusal in `test_role_values_through_command_load`. Context in `test_each_request_setting_partitions_context`. Wire stays `response_format` in `test_unimplemented_transport_override_leaves_builder_wire_format`. Command refuses the non-default before sends in `test_loaded_naming_transport_override_refused_before_sends`. | configurable, existing wire format protocol-bound |
| Provider/model/engine/endpoint/engine extras/fallback | `vlm.backends.*.{provider,engine,model,base_urls,endpoints,extra_body,allow_images,on_budget_exhausted,call_budget}`, `vlm.default_backend` | Provider/engine coherence and route validation, last-write extras, final ordered-pair structural validation before admission, fallback re-proves | Examples and deployment overlay | Loader refusals in `test_engine_profile_override_and_refusal`, `test_unsafe_url_refused_at_profile_load` and `test_binding_rules_through_profile_load`. Context in `test_each_request_setting_partitions_context`. Wire URL, model and extras in `test_loaded_profile_composes_role_client`, extras precedence in `test_ordered_pair_uses_role_builder_and_extra_body` and fallback in `test_fallback_backend_probe_and_generation_images_use_its_own_side`. `test_loaded_ordered_shape_override_reports_probe_failure` loads both a sampling extra and `messages`, verifies both on the ordinary wire, then observes the closed ordered refusal with spare and exhausted allowance. `test_probe_pair_outcome_property` generates further invalid final shapes. See [Fix round 2](#fix-round-2) for execution. | configurable, ordered shape protocol-bound, credential exclusion safety-bound |
| Image source/side/format/quality/decode/alpha | `vlm.roles.*.image.{source,max_side,format,quality}`, `vision.*`, `vlm.backends.*.image.*` | Shared preparation and source permission, decision 144 side rule | Both examples and deployment overlay | Loader refusal in `test_image_members_required_and_max_side_can_inherit` and `test_alpha_policy_profile_override_and_refusal`. Context in `test_each_request_setting_partitions_context`. Sent side and bytes in `test_loaded_profile_probe_images_over_loopback_obey_the_side_rule`, `test_loaded_profile_composes_role_client` and `test_prepared_image_provenance_matches_final_sent_payloads`. The alpha test asserts configuration acceptance/refusal. | configurable, image hashes artifact-bound |
| `image.transport` | `vlm.roles.*.image.transport` | Schema accepts nonempty alternate strings for the manifest-drafter round trip, naming command requires `data_uri` | Both examples | Loader acceptance/refusal in `test_role_values_through_command_load`. Context in `test_each_request_setting_partitions_context`. Wire stays `data:` in `test_unimplemented_transport_override_leaves_builder_wire_format`. Command refuses the non-default before sends in `test_loaded_naming_transport_override_refused_before_sends`. | configurable, existing data URI protocol-bound |
| Probe cap/timeout, HTTP retry/deadline, concurrency and grant | `vlm.backends.*.{probe,http,max_concurrency,call_budget}` | Actual-send accounting, ledger and locks; shared backend cap | Both examples | Loaded allowance and timeout effects in `test_loaded_probe_allowance_and_lazy_role_extension` and `test_loaded_probe_timeout_changes_observed_outcome`. `test_lazy_proof_uses_configured_deadline_without_retrying_unknown_result` observes the exact ordered timeout override and cancellation of both transport attempts. Retry/deadline and concurrency behavior in `test_loaded_profile_composes_role_client`. Refusals in `test_probe_profile_missing_sentinel_and_invalid_refused`, `test_backend_operational_schema` and `test_production_budget_configuration_refusal`. Retry, grant and partial groups in `test_retries_are_actual_sends_and_blocked_send_does_not_spend_cloud_grant` and `test_probe_pair_outcome_property`. | configurable, allowance and ownership safety-bound |
| Proof identity, verdicts and sanitized diagnostics | Closed code and artifact contract | Canonical context, group precedence and typed replies | No profile value | `test_hostile_reply_and_diagnostics_are_closed`, `test_probe_pair_review_failure_scenarios`, `test_ordinary_proof_keeps_each_typed_reason`, `test_ordered_pair_uses_role_builder_and_extra_body` and `test_typed_failure_reaches_checkpoint_and_stage_summary`. | artifact-bound, protocol-bound and safety-bound |
| Paths, device, installation, storage and lifecycle | Existing containment, paths and runtime resource settings | Existing anchored writers and owned teardown | No profile change | Phase 3 exercises CPU double teardown and basetemp cleanup. Standalone anchored-write, device, installation and live service checks are `not_run` in this round because the change introduces no path, device or service settings. | containment and ownership safety-bound |

New literal inventory by owning role: profile member lookups are configurable; provider/engine enums, HTTP/JSON field names, data-URI delimiters, completion labels and `PROBE_*` codes are protocol-bound; `role_probe_context_v1`, challenge schema/value labels, bundle IDs, context/evidence members and SHA-256 digests are artifact-bound; the explicit production-versus-injected mapping policy, strict integer checks, no fabricated semantic proof and shared allowance are safety-bound; generated values/fixtures are test-only. No retained literal is unclassified or a known defect.

## Handoff

- **Current status:** REVIEWING; implementation done, STOP for PM's Gemini and fresh Codex reviews.
- **What was already done:** five approved steps, coherent fixture sweep, prepared-image provenance, 37 serial green file runs, 3126 passed/41 existing skips, eight killed mutants and executed configuration audit.
- **What the next agent must do:** reviewers inspect this patch and retained evidence; PM integrates, runs its full suite/CI, and selects any next smoke allowance. No live run is authorized here.
- **What must not be repeated:** no unrequested live call, profile retuning, silent token/cap increase, borrowed backend-only proof, synthesized semantic capability, install or worker Git write. Do not rerun passing focused checks without a new change/failure.
- **Worktree/base/patch:** `.tmp/worktrees/KBS-F2`, supplied `0220123`; source/test/docs changes are uncommitted for PM ownership. No Git write or worker-created Git patch.
- **PM decisions/notes:** Q1-Q4 implemented under explicit approval; PM owns decision records and the next dev cap. Dev all-role ordered schedule remains 20 sends before retries.
- **Lessons learned:** construct provider-dependent fixture identity in one loader-validated helper; use the bound client variable actually in scope; sweep sibling stage assertions when typed codes replace a generic error. The initial stop was honored, then resumed only on PM dispatch.

## Worker completion

- [x] Changes: Shared probe/real-call builder, context-isolated proofs, typed failure diagnostics and prepared-image digests completed; coherent provider fixtures and affected stage assertions updated; no profile retuning or Git writes.
- [x] Checks: **37 files, 3126 passed, 41 existing skips; 8/8 mutants killed**. Configuration audit executed. Ruff lint/format not_run because the project environment has no Ruff; no install.
- [x] Cleanup: Owned checks exited; fixed basetemp and mutant copies removed per run. Removed runners, recall database, cleanup script and duplicate per-run JSON. Verification: `CLEANUP: PASS; basetemp, mutant copies, runners and recall database absent; small evidence reports retained`; cleanup-script absence check returned `False`. Raw check logs and consolidated reports remain as evidence.
- [x] Remaining: PM's Gemini/fresh Codex reviews, integration and full suite/CI; any live smoke requires its own dispatch and allowance. No known open implementation defect.

## Fix round 1

### Step 0

**Status at plan handoff** `STEP_0_COMPLETE`. The PM approved all four proposals for phase 3. R-015, R-018, R-020, R-026. The phase 2 review supersedes the earlier completion status and its checks describe the pre-fix patch.

#### Unknowns and failure modes

- The role schema accepts effort values beyond the pinned vLLM enum. A test using the shared mapper as its oracle can miss a field both callers emit incorrectly. The scope of the effort rule matters because cloud reasoning predates the local `mapped` policy.
- A group can be cut before execution or during a retry. Both paths must distinguish an unfinished group from failed transport and preserve earlier case diagnostics. Raw low-level exception names are used by legacy injected tests, so trace normalization belongs at the shared production aggregation boundary.
- Refusal fields can have invalid falsey values as well as truthy ones. OpenAI can return a nullable refusal, Gemini can return feedback without candidates, and finish metadata can classify an envelope even when it carries no answer content.
- Cached proofs feed generation records as well as failure events. Evidence must capture both orderings and their mapping warnings without storing provider text. Output and image transports can affect identity or be gated by the command even when the shared builder emits the current fixed format.

#### Ranked questions and proposed answers

1. **Which engine evidence qualifies effort for emission?** Use the exact configured vLLM `0.11.0` identity with `low`, `medium`, `high` from N057. Omit other non-sentinel values on unmapped local engines or versions with `PARAM_UNSUPPORTED`. Preserve injected semantic evidence under its existing policy. Cross-check existing cloud effort writers as part of the sweep. Preserve their pre-existing behavior, subject to PM confirmation that C1 targets the new local `mapped` branch.
2. **What wins if a group sees both a terminal transport error and an allowance cut?** Propose `PROBE_FAILED` for a terminal transport failure, `PROBE_INCOMPLETE` for a cut without terminal failure, then the first typed negative in challenge order for a fully observed group. A retry cut in the same case is incomplete under the existing actual-send contract. Keep required capabilities in their current order and report closed reasons for every case, including unsent cases.
3. **Which refusal shapes count?** Propose missing or null OpenAI refusal as absent, a string as the protocol value, and any other supplied type as malformed. Enforce a string for a present Gemini `blockReason`. Keep `content_filter`, `SAFETY` and provider stop classifications independently usable. Validate typed members at the point where they are interpreted, including Anthropic part type and text, with no prose matching.
4. **What counts as a transport override check?** Use the existing naming or observation command gate with a loaded override and assert keyed refusal before sends. Keep a separate schema round-trip or context assertion where supported. Record current builder behavior and defer new transport mapping as guided by C5.

May assume the existing challenge bundle, order of required capabilities, injected test seams and shared send accounting. Must ask before any new key, wider provider mapping, change in fallback meaning, deployment identity, spend or profile retuning.

#### Research evidence and class sweep

| Finding | Source and evidence | Implementation boundary |
|---|---|---|
| C1 | `docs/note/N057-vllm-openai-server.md:12` records the vLLM v0.11.0 source excerpt. `config.py:657` resolves exact engine identity from loaded settings. `model_mapping.py:18–80` shows current sampling, switch, budget, output, image and extras precedence. `model_cloud.py:31–68` and `model_mapping.py:83–109` show existing provider-specific construction. | Trace ordinary probes, ordered pair and real requests through `openai_request`, and ordinary cloud probes and real requests through `cloud_request`. Use N057 as the PM-specified local evidence. Check for locally available pinned source before implementation and record availability. External primary rereads are unavailable within the network limit. |
| C2 | `model_probe.py:271–329,428–464` and `model_client.py:428–533` show typed case results, duplicate group summaries and a separate no-send branch. `model_transport.py:50–85` charges retries individually. `test_model_production_probes.py:705–747` covers a retry cut. | Route ordinary, ordered and zero-send group summaries through one rule. Preserve raw injected helper behavior where existing callers depend on it. Check caller-facing code, case status, actual sends, and unchanged required-capability order. |
| C3 | `model_probe.py:118–178` shows the truthiness reads. Local T-009 `provider-docs.md:43–47` records refusal members and finish categories. Phase 2 review C3 reproduces both bad shapes and the independent OpenAI finish category. | Check each provider envelope member read on the verifier path and validate required structure before interpretation. |
| C4 | `model_client.py:326–347,450–479,523–533,793–794` shows discarded pair warnings, event-only ordinary case reasons, and the proof reader on the call record. `safe_probe_events` keeps closed labels at lines 39–64. | Keep case evidence and aggregate reason from the same normalized result. Record pair warnings and the matching context hash with body/image hashes, including for an allowance-cut proof. Check proof, failure event and call-record readers. |
| C5 | `test_model_config.py:231–275` exercises schema-level values for the manifest drafter. `test_probe_request_settings.py:114–147` exercises identity. `config.py:1209,1224,1279,1287` gates active K2 transports, while the builders choose wire shape by the provider and requested format. | Check every historical audit row against exact assertions. Report loader, context, wire and pre-send refusal evidence separately. |

Sources above are locally read project code or archived notes. `recall.py build --db .tmp/KBS-F2-fix/recall.sqlite` returned `indexed=94 unchanged=0 removed=0 refused=0 chunks=689 skipped_chunks=0`. Searches for `reasoning_effort`, `probe allowance`, `N057` and `engine_native` surfaced prior reasoning and allowance notes, including N057, with no `engine_native` result.

#### Configuration impact

| Settings or behavior | Validation and profile source | Proposed check and literal class |
|---|---|---|
| `vlm.roles.*.reasoning.effort`, `vlm.backends.*.{provider,engine}`, model identity and engine extras | `vlm_role.v0` and `enhancer_config.v0` schemas, `load_config` and `resolve_backend_engine`. Base and dev examples and deployment overlays supply existing values. | Load documented vLLM with supported and unsupported effort, exercise unlisted version/engine and invalid settings, and assert ordinary, ordered and generation bodies or warnings. Preserve model identity, endpoint and extras precedence. Settings are configurable, exact version/value support and warning names are protocol-bound. |
| Role cap, other reasoning controls, sampling and stop/seed, output/schema, image preparation including transport, side and format, backend image limits and `vision` | Same schemas and profiles. Existing role validation and command gates in `config.py`. | Reuse wire and refusal checks for settings affected by shared construction. Add a loaded `output.transport` refusal before sends, and substantiate current wire behavior independently of schema acceptance or context identity. Settings are configurable. Challenge schema and fixture/image digests are artifact-bound. |
| `vlm.backends.*.{probe,http,max_concurrency,call_budget,on_budget_exhausted}` and fallback | `enhancer_config.v0` and existing validators/profile examples. | Mixed outcomes, zero or partial group and retry-cut checks assert actual sends and exact codes with the existing cap and lock. Operational values are configurable. Allowance/cancellation behavior and diagnostic filtering are safety-bound. Status and closed reasons are protocol-bound. |
| Proof evidence, context identity and reply classifier | Existing context digest, closed codes and recorded proof fields. | Assert safe case reasons, warnings, context and retained hashes. Evidence names/hashes are artifact-bound. Reply member types and finish labels are protocol-bound. |
| Paths, storage, cache locations, device/topology, dtype, RAM/VRAM, workers/batch limits and service lifecycle | Existing containment, schema and profile settings. | No change in configuration reads or operational behavior is proposed. Use project venv and task-owned scratch with bounded CPU subprocess cleanup. |

#### Plan and finish checks

1. **Pending approval.** Add failing-first tests for the loaded vLLM enum and warnings, mixed HTTP 503/malformed and partial `length` pairs, wrong-type refusal members, and missing proof provenance. Add a focused scratch assertion for the C5 reporting defect and test the existing loaded output transport gate. The reporting assertion should fail against the old audit. The existing transport gate is expected to pass.
2. Implement C1–C4 within the existing request, probe and client modules. Extend the hostile-envelope property with protocol-shaped wrong types and exact expected codes. Generate mixed case outcomes for both paths and assert codes, case evidence and send counts independently. Rerun the red-first checks against the fixes.
3. Run the eight assigned files through `tools/agent-workflow/test_slot.py`, one file/process. Run 6–10 scratch-only mutants covering the version/effort gate, summary precedence, allowance cut, refusal typing and proof evidence. Use one fixed basetemp under `.tmp/KBS-F2-fix/`, faulthandler, hidden bounded child processes and cleanup after each run. Capture commands, exact outputs and skip or `not_run` reasons.
4. Record cause, fix and checks for C1–C5 here. Update the Configuration audit row by row with confirmed effects and classify literals. Record any pre-existing transport wire limitations under `Noticed, not done`. Retain compact evidence, remove owned scratch and report to the PM for review.

**Step 0 completion.** This section is the only worktree edit. Red-first tests, runtime edits, final Configuration audit, focused tests, mutations and live checks are `not_run` at this approval gate.

### Steps

- **Step 1 complete.** Before any runtime edit, the focused regression selection returned `19 failed, 7 passed, 103 deselected in 15.94s`. Failures reproduced unsupported effort, wrong mixed/partial error codes, wrong refusal types and missing proof evidence.
  The separate C5 check exited 1 with `AssertionError: output.transport audit lacks separately evidenced claims`. Both CLI transport refusal cases exercised the existing gate.
- **Step 2 complete.** The local mapping, group summaries, envelope typing and retained evidence are implemented. The focused rerun returned `26 passed, 103 deselected in 5.08s`.
  The C5 rerun printed `C5_AUDIT PASS: both transport rows identify loader, context, wire and command refusal evidence` and exited 0.
- **Step 3 complete.** Eight files passed serially after the final runtime edits, with 479 passes. All nine scratch mutants had the intended regression failure.
  The direct review-envelope rerun reproduced both malformed codes and the independent content-filter refusal. The initial client deadline is recorded below.
- **Step 4 complete.** The Configuration audit and findings evidence were updated for phase 3. Exact successful file summaries, the initial deadline and the mutation failures are recorded below.
  The worktree is ready for PM review.

### Completion report for phase 3

| Finding | Cause and change | Red and green evidence |
|---|---|---|
| C1 | `model_mapping.py:19,65` binds local mapped effort to the documented vLLM `0.11.0` enum. Other non-sentinel role values produce `PARAM_UNSUPPORTED`. All three local writers use this builder. | The loaded `max` double produced `ModelCallError: PROBE_FAILED` before the fix. The supported values are present on all eight probes and the real call after the fix. `max`, `xhigh`, an unlisted vLLM version and a different local engine omit the body member and warn in every proof and the call. |
| C2 | `model_probe.py:289,501` summarizes each required group from its cases. `model_client.py:446,493` uses that outcome for ordinary, ordered and unsent groups. Existing required-capability selection order stays in `model_client.py:687–707`. | Before the fix the mixed pairs returned `PROBE_MALFORMED` and partial pairs returned `PROBE_TRUNCATED`. Both paths now preserve the initial cause and all case codes as shown below. The generated test checks a pass, typed negative, terminal 503 or allowance cut with actual send counts independently of the group helper. |
| C3 | `model_probe.py:118–193` checks the envelope containers and optional refusal members before interpreting them, including Anthropic content-part type. A valid OpenAI `content_filter` or Gemini safety signal can decide a reply without answer text. | Object refusal fields returned `PROBE_REFUSED` before the fix and falsey wrong types could pass. The property and explicit structural checks now yield `PROBE_MALFORMED`. The independent `content_filter` regression is green before and after. |
| C4 | `model_client.py:450–472,505–520` stores normalized ordinary case reasons and ordered mapping warnings and context hash alongside body and prepared-image hashes. The bound call retains the proof at `model_client.py:777`. | Ordinary evidence raised `KeyError: 'challenges'`. Ordered call evidence raised `KeyError: 'context_hash'` and the loaded supported-effort cases reached `KeyError: 'warnings'`. The corrected tests verify two distinct ordinary reasons, exact ordered warnings, matching context identity and an actual successful call record. The cut-pair property covers unsent proof evidence. |
| C5 | The Configuration audit names separate loader, context, wire and pre-send refusal assertions, including two transport rows. | The scratch audit check was red with `AssertionError: output.transport audit lacks separately evidenced claims`. It now prints `C5_AUDIT PASS: both transport rows identify loader, context, wire and command refusal evidence`. Both command transport refusals and existing builder body checks are green on both revisions. |

#### Class sweep results

- **Request writers.** `model_probe.production_probe_cases`, `RoleClient._multiple_images_pair` and `RoleClient._call` share `openai_request`. Loaded local `engine.name` and `.version` resolve directly from the backend values (`config.py:657`). Top-k/min-p and the chat-template switch already have the local engine gates in `model_mapping.py:21–63`. Budget and repetition penalty still warn. Output, image format, model identity and extras continue through the same body construction. Provider extras still merge last. The legacy injected semantic-control path consults its supplied evidence. The two cloud call sites use `cloud_request`, where `map_cloud_reasoning` writes Anthropic `output_config.effort` or Gemini `generationConfig.thinkingConfig.thinkingLevel`. Their pre-existing rules are distinct from the local `mapped` policy. N057 is the PM-approved archived vLLM evidence. The checked project venv and T-009 evidence paths had no local pinned provider source. No external fetch was run.
- **Group writers.** The ordinary loop, ordered pair and ordered zero-send branch use `summarize_probe_group`. The same case reason drives `errors` and the group event. The underlying raw `probe_capabilities` keeps its exception-name contract for injected callers including standalone single-image and thinking pairs. An allowance cut during a retry stays incomplete. The actual-send debit, grant/cancellation path and required-capability order are preserved. The phase 3 CLI contract still asserts 20 sends for the full synthetic demand.
- **Envelope readers.** The verifier checks top-level objects, choice/candidate lists and member objects before field access. The finish members are strings, refusal presence permits OpenAI null, and a present Gemini block reason must be a string. Anthropic part type is checked before selecting text parts. `str.join` rejects non-string text parts in Anthropic and Gemini, and final answer parsing still requires an exact integer object. Verified by the hostile property and direct shape regressions.
- **Evidence readers.** Ordinary/ordered event reasons flow through `safe_probe_events` into `k2_naming.record_probe_failure` and `kb_sync.require_success`. Generation attaches the run proof from its resolved context. Cached ordered warnings and context hash persist even if the allowance prevents the pair from completing. Events and proofs omit raw response text, finish strings and exception messages. The focused tests cover durable checkpoint and `STAGE_FAILED` evidence.
- **Configuration audit.** Every previous row was checked against the named assertions. HTTP and concurrency overrides observe admission/deadline behavior. Loader and context tests remain separate for transport values and other role settings. The existing command gate supplies the pre-send transport refusal.

#### Exact review scenario output

```text
image ('transport', 'malformed') cap=4: PROBE_FAILED, cases=[{'challenge': '0', 'status': 'failed', 'reason': 'PROBE_FAILED'}, {'challenge': '1', 'status': 'negative', 'reason': 'PROBE_MALFORMED'}]
multiple_images ('transport', 'malformed') cap=8: PROBE_FAILED, cases=[{'challenge': 'first', 'status': 'failed', 'reason': 'PROBE_FAILED'}, {'challenge': 'second', 'status': 'negative', 'reason': 'PROBE_MALFORMED'}]
image ('truncated', 'pass') cap=3: PROBE_INCOMPLETE, cases=[{'challenge': '0', 'status': 'negative', 'reason': 'PROBE_TRUNCATED'}, {'challenge': '1', 'status': 'incomplete', 'reason': 'PROBE_INCOMPLETE'}]
multiple_images ('truncated', 'pass') cap=7: PROBE_INCOMPLETE, cases=[{'challenge': 'first', 'status': 'negative', 'reason': 'PROBE_TRUNCATED'}, {'challenge': 'second', 'status': 'incomplete', 'reason': 'PROBE_INCOMPLETE'}]
vllm 0.11.0 effort=low: wire=low, warned=False
vllm 0.11.0 effort=medium: wire=medium, warned=False
vllm 0.11.0 effort=high: wire=high, warned=False
vllm 0.11.0 effort=max: wire=None, warned=True
vllm 0.11.0 effort=xhigh: wire=None, warned=True
vllm 0.33.0 effort=high: wire=None, warned=True
llama_cpp 0.11.0 effort=high: wire=None, warned=True
openai_compatible object refusal: PROBE_MALFORMED
gemini object blockReason: PROBE_MALFORMED
openai_compatible content_filter alone: PROBE_REFUSED
```

#### Serial verification

The scratch runner executed the real slot limiter with `runpy`, adding `CREATE_NO_WINDOW`, a 180-second child deadline, captured output and basetemp cleanup. `PY` below is `<project>/.venv/Scripts/python.exe`. Tests ran with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src` and scratch-local Hypothesis storage. `BASE` is the resolved `<worktree>/.tmp/KBS-F2-fix/basetemp`. Each command held the slot for one process. Evidence class is CPU unit and mock contract, with labelled transport doubles.

```text
PY -B -X faulthandler .tmp/KBS-F2-fix/run_checks.py red|green|suite|client|envelopes
tools/agent-workflow/test_slot.py -- PY -B -X faulthandler -m pytest -q --tb=short -p no:cacheprovider tests/<one-file> --basetemp=BASE
```

The red and green actions select the named regressions with `-s -k` and run `check_audit.py` in a separate slot. The envelopes action runs `review_envelopes.py` in its own slot. The model-client rerun adds `-vv -o faulthandler_timeout=45` to the same single-file command.

| Pytest file | Final output | Exit |
|---|---|---|
| `test_probe_request_settings.py` | `129 passed in 21.96s` | 0 |
| `test_model_production_probes.py` | `84 passed in 31.56s` | 0 |
| `test_kb_sync_production_probes.py` | `1 passed in 6.60s` | 0 |
| `test_model_client.py` | `79 passed in 20.15s` | 0 |
| `test_model_mapping.py` | `4 passed in 0.06s` | 0 |
| `test_model_probe.py` | `5 passed in 0.23s` | 0 |
| `test_model_composition.py` | `54 passed in 54.74s` | 0 |
| `test_model_config.py` | `123 passed in 26.32s` | 0 |

The initial `test_model_client.py` process reached 75 result markers and the child deadline. The runner recorded `PID 26568: deadline exceeded`, exit 1. The diagnostic rerun passed all 79 without a dump. The cause of the initial timeout was not established. The eight final runs total **479 passed**.

#### Scratch mutations

`PY -B -X faulthandler .tmp/KBS-F2-fix/mutate.py` invoked the real limiter once per row. Each run used `PY -B -X faulthandler .tmp/KBS-F2-fix/mutant_bootstrap.py prompt_enhancer.<module> .tmp/KBS-F2-fix/mutant.py` with the same pytest flags for `tests/test_probe_request_settings.py`, the fixed basetemp and the listed `-k` expression. The bootstrap imported the one modified scratch module and read all other source in place. Each pytest child exited 1 with the intended failure.

| Modified behavior | Selection | Captured summary |
|---|---|---|
| Accept vLLM `max` | `loaded_reasoning_effort and max` | `1 failed, 128 deselected in 1.60s` |
| Ignore engine version | `loaded_reasoning_effort` | `1 failed, 6 passed, 122 deselected in 2.78s` |
| Prefer typed negative over transport | `review_failure_scenarios and PROBE_FAILED` | `2 failed, 127 deselected in 1.34s` |
| Prefer truncation over allowance cut | `review_failure_scenarios and PROBE_INCOMPLETE` | `2 failed, 127 deselected in 1.53s` |
| Ignore falsey OpenAI refusal types | `hostile_reply` | `1 failed, 128 deselected in 1.15s` |
| Ignore falsey Gemini block reasons | `refusal_envelope_types and payload2` | `1 failed, 128 deselected in 0.80s` |
| Drop ordinary case reasons | `keeps_each_typed_reason` | `1 failed, 128 deselected in 1.43s` |
| Drop ordered mapping warnings | `ordered_pair_uses_role_builder` | `1 failed, 128 deselected in 1.52s` |
| Drop ordered context identity | `ordered_pair_uses_role_builder` | `1 failed, 128 deselected in 1.51s` |

The runner's text matcher counted seven automatically. Two used pytest's rewritten assertion text without the literal `AssertionError`. The captured failures show `assert False` when an unlisted version emitted effort and `assert None == 'PROBE_MALFORMED'` for a boolean refusal. Both hit the named assertions. Final result **9/9 killed**, zero survivors and zero collection failures.

#### Static checks, limits and handoff

- **Work completed.** Updated `src/prompt_enhancer/{model_mapping,model_probe,model_client}.py`, `tests/test_probe_request_settings.py`, `contracts/vlm-request.md` and this task file in the uncommitted worktree at base `0220123`. The fix uses the existing role and backend settings with their existing profiles and schemas. New enum literals and case codes are protocol-bound. The context, case evidence names and hashes are artifact-bound. Normalizing exception causes to closed categories and preserving send accounting are safety-bound. Synthetic inputs are test-only.
- **Static checks.** Scoped `git diff --check` exited 0 with empty output. `PY -B -I -m ruff check <changed Python paths>` and `PY -B -I -m ruff format --check <changed Python paths>` each exited 1 with `<project>\.venv\Scripts\python.exe: No module named ruff`. Ruff lint and format are `not_run` because Ruff is unavailable in the project venv.
- **Other `not_run`.** Full suite and CI are PM integration checks. Live provider, GPU, deployment/performance, external evidence retrieval and installation were outside this phase. The evidence is CPU unit and mock contract only.
- **Cleanup.** Each child exited and the runner removed basetemp after each launch. Final checks returned `False` for the basetemp and mutant copy, found zero test slot locks and returned `False` for `.tmp/KBS-F2-fix` after removal. This task file retains the check output.
- **Current status.** `REVIEWING`, with the approved C1–C5 fix round complete. PM can dispatch independent review and run the full suite in integration. No worker Git writes.

#### Noticed, not done

- `output.transport` and `image.transport` affect the role schema or proof identity where accepted. The local builder still emits its existing `response_format` and `data:` image URI for the same structured request and prepared image. The naming command refuses alternate transport values before sends. New wire policies require separate scope.
- T-009 notes model-dependent Gemini effort support. The existing cloud mappings were outside C1's approved local effort rule and were left as recorded in the class sweep.

## Fix round 2

### Step 0

R-015, R-018, R-020, R-026. Small task. Step 0 is recorded before implementation under the phase 4 continuation instruction.

#### Unknowns and failure modes

- The final ordered body may lose the image parts or their order after the builder merges extras. Image preparation can also fail before a full pair exists. The pair must be checked with zero, one or two sends remaining, and an invalid pair must spend zero ordered sends.
- Eager binding can retain a failed proof for the required-capability gate. Lazy and fallback calls must use that proof and attach the same sanitized event. A cancellation or escaping budget refusal must retain the previous retryability semantics.
- Direct injected helpers validate caller-supplied plans and have callers expecting `ValueError`. Cached production evidence must stay context-scoped and avoid recording raw error or configured text.

#### Ranked questions and proposed answers

1. **Which policy fixes the report?** Choose option (a). `contracts/vlm-request.md:10` documents last-write extras, and the backend schema at `contracts/schemas/enhancer_config.v0.schema.json:3505` permits open object members. Reject a production ordered pair structurally during preparation using existing `PROBE_FAILED`. A new loader reserved-key list would change the existing extras contract and affect unrelated providers and calls.
2. **How does the invalid-pair result reach callers?** Aggregate the failed pair into the existing proof result and safe event, with no ordered sends. Preserve known-good ordinary proofs and their demand order. Eager binding records the failure and the later required call reports it, matching the treatment of failed HTTP challenges. Retain context and available mapper warnings. Body and image hashes describe valid prepared challenges only.

May assume the existing capability, fallback, extras, budget and profile policies. Ask before changing those policies, adding a key or changing model identity or spend.

#### Research evidence and class sweep

- **Wire writers.** `model_mapping.py:21–90` sets local `max_tokens`, sampling fields (`temperature`, `top_p`, `top_k`, `min_p`, presence/frequency penalties, seed, stop), `chat_template_kwargs`, `reasoning_effort`, `model`, `messages` including image URLs and optional `response_format`. `model_cloud.py:31–68` sets Anthropic `model`, `system`, `messages` including image source, `max_tokens`, `stop_sequences`, `thinking`, `output_config` and Gemini `systemInstruction`, `contents`, `generationConfig` containing token limit, sampling and thinking. Their merge-last extras can replace any top-level member and nested structure via that member. Headers and endpoints are built separately. Existing embedding extras have a separate contract.
- **Preparation and allowance.** `model_client.py:381–436` handles bundle and ordinary challenge building before counting. `:326–360,480–520` divides ordered building, counting and validation across two call sites. Move ordered validation before the count inside production preparation. The lazy call and both fallbacks reach the same production context (`:641–687`). Generation guards manifest, image sources and prepared bytes, and the public wrapper codes configuration, input and client-state failures (`:598–757`). Handcrafted injected image/thinking/multiple-image plans keep their existing signatures and behavior. Their readers are in `test_k2_probe.py` and `test_k2_fix_round{2,3}.py`.
- **Review evidence.** `.tmp/KBS-F2-rev2/review-codex.md` records raw eager `ValueError`, lazy `INPUT_INVALID` without evidence and exhausted `PROBE_INCOMPLETE`, all after six ordinary sends in a loaded profile. The local recall build returned `indexed=94 unchanged=0 removed=0 refused=0 chunks=689 skipped_chunks=0`. Searches for `extra_body` surfaced N057 and STATUS. `ordered pair` had no hits. The project sources and archived review suffice for this structural change.

#### Configuration impact

| Setting or behavior | Validation and profile source | Proposed check and literal class |
|---|---|---|
| `vlm.backends.*.extra_body`, role image prep and backend image limits | Existing backend and role schemas. Base/dev examples and overlays retain last-write extras. | Loaded shape-changing extra and valid sampling precedence. Ordered pair invariants apply to final bodies before counting. Configurable values, protocol-bound shape checks, artifact-bound evidence. |
| `vlm.backends.*.probe.{max_requests,timeout_s}`, retry, grants, fallback and concurrency | Existing backend validators and examples. | Counts at cap 6 and 8 and generated valid/invalid outcomes. Existing caps and budgets remain configurable. Accounting and closed diagnostic filtering are safety-bound. |
| Model/engine, sampling/reasoning/output mappings, paths, device, RAM/VRAM, storage and lifecycle | Existing schemas, base/dev examples, overlays and containment. | Existing behavior remains in scope for regression checks. The fix adds no operational key or profile value. |

#### Plan and finish checks

1. Run loaded-profile eager cap 8, lazy cap 8 and eager cap 6 regressions before the runtime edit. Record observed error, sends and attached evidence in each variant.
2. Validate built ordered cases before allowance. Feed preparation failures through the group/proof/event path. Extend the mixed-outcome property with an unpreparable ordered pair while keeping normal outcomes and actual-send checks.
3. Rerun the three variants and all ten assigned pipeline files serially through the limiter. Exercise 3–6 scratch mutants, record exact summaries and update the extras audit row. Remove task-owned scratch after the checks.

### Steps

- **Step 1 complete.** Loaded-profile tests reproduced the three review variants with `3 failed, 129 deselected in 2.05s` before runtime edits.
  The output records the exact error, ordinary sends, remaining allowance and attached events per variant.
- **Step 2 complete.** Production case construction validates the final pair before counting sends. Preparation failures use the same case and capability summary as sent outcomes.
  All three variants and the expanded mixed-outcome property passed in `4 passed, 128 deselected in 5.12s`.
- **Step 3 complete.** The ten assigned files passed serially with 552 passes and all four scratch mutants were killed. Three additional direct K2 reader files passed after test-only fixture corrections.
  The extras audit row records the loaded sampling override and the closed ordered structural refusal. Verification and cleanup are recorded below.

### Completion report for phase 4

- **Cause, REV2-01.** The production client merged backend extras into both ordered bodies, then acquired the allowance count before validating the pair. A shape-changing extra therefore raised out of eager preparation, reached lazy handling without proof evidence, or appeared as a budget cut at zero remaining sends.
- **Fix, option (a).** `multiple_images_probe_cases` validates the final bodies and constructs the ordered response verifiers synchronously. Production calls it before checking the allowance. A build or shape failure enters the existing group summary as `PROBE_FAILED` for both cases. The run caches the failure and records the safe event, context and available mapping warnings. The capability remains unproved and the event reports the observed send counter. Eager binding can return a bound client with the failed proof, and both eager and lazy required calls attach that evidence to `ModelCallError`. The injected wrapper continues to use the same pure pair validator and its prior signature.
- **Scope decision.** `contracts/vlm-request.md` documents merge-last extras. Option (a) enforces the ordered challenge invariant on the final bodies and preserves existing loader acceptance and sampling precedence. `test_loaded_ordered_shape_override_reports_probe_failure` loads both `messages` and a temperature override and observes them on all six ordinary sends. Neither invalid ordered request is sent.
- **Class sweep.** The ordinary group checks pinned artifacts, resolves image sides, prepares its own body before allowance and codes expected build errors. Backend resolution, schema and header preconditions remain separate from group preparation. The effective backend branches for image and budget fallback re-enter the same production path. Generation checks the manifest, image source, image preparation and schema, then uses the shared request builder under its public error wrapper. Directly injected plans enter their existing wrapper after admission and retain their caller contract. The provider mappings, challenge verifier, required-capability order and vocabulary from C1–C5 are unchanged. Valid ordered cases still use `probe_backend`, which debits the transport per actual attempt.
- **Configuration audit.** The backend/extras row above now records the loaded last-write override and post-merge structural refusal, with independent actual-send assertions. No schema, profile, role or backend value changed. Closed codes and case classification are protocol-bound, proof evidence is artifact-bound, resend prevention and sanitization are safety-bound, and generated values are test-only.

#### Exact REV2-01 output

Before the runtime edits, the three assertions failed as expected.

```text
ordered override cap=8 eager=True: type=ValueError code=None sent=6 remaining=2 ordered_events=0 attached=0
ordered override cap=8 eager=False: type=ModelCallError code=INPUT_INVALID sent=6 remaining=2 ordered_events=0 attached=0
ordered override cap=6 eager=True: type=ModelCallError code=PROBE_INCOMPLETE sent=6 remaining=0 ordered_events=1 attached=5
3 failed, 129 deselected in 2.05s
```

After the runtime edits, all three variants and the mixed-outcome property passed.

```text
ordered override cap=8 eager=True: type=ModelCallError code=PROBE_FAILED sent=6 remaining=2 ordered_events=1 attached=5
ordered override cap=8 eager=False: type=ModelCallError code=PROBE_FAILED sent=6 remaining=2 ordered_events=1 attached=5
ordered override cap=6 eager=True: type=ModelCallError code=PROBE_FAILED sent=6 remaining=0 ordered_events=1 attached=5
4 passed, 128 deselected in 5.12s
```

#### Verification command

`PY` is `<project>/.venv/Scripts/python.exe`. The scratch runner invokes the actual limiter using `runpy` and provides `CREATE_NO_WINDOW`, a 300-second child deadline, stdout capture and cleanup of the fixed `BASE=<worktree>/.tmp/KBS-F2-fix/basetemp`. Test children use `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src` and task-local Hypothesis storage. Evidence class is CPU unit and mock contract using labelled doubles.

```text
PY -B -X faulthandler .tmp/KBS-F2-fix/run_checks.py red|green|suite
tools/agent-workflow/test_slot.py -- PY -B -X faulthandler -m pytest -q --tb=short -p no:cacheprovider -o faulthandler_timeout=60 tests/<file>.py --basetemp=BASE
```

Red adds `-s -k loaded_ordered_shape_override`. Green adds `-s -k "loaded_ordered_shape_override or probe_pair_outcome_property"` to the same one-file command.

| Assigned file | Final output | Exit |
|---|---|---|
| `test_probe_request_settings.py` | `132 passed in 16.77s` | 0 |
| `test_model_production_probes.py` | `84 passed in 15.65s` | 0 |
| `test_kb_sync_production_probes.py` | `1 passed in 3.89s` | 0 |
| `test_model_client.py` | `79 passed in 13.53s` | 0 |
| `test_model_mapping.py` | `4 passed in 0.04s` | 0 |
| `test_model_probe.py` | `5 passed in 0.13s` | 0 |
| `test_model_composition.py` | `54 passed in 45.04s` | 0 |
| `test_model_config.py` | `123 passed in 18.05s` | 0 |
| `test_model_transport.py` | `42 passed in 3.18s` | 0 |
| `test_kb_sync_flow.py` | `28 passed in 75.28s (0:01:15)` | 0 |

The assigned suite finished with **552 passed**, zero skips. It includes the existing C1–C5 assertions and the CLI double's 20-send check.

#### Scratch mutants

`PY -B -X faulthandler .tmp/KBS-F2-fix/mutate.py` used the same limiter and fixed basetemp. Each child imported only a scratch copy of `model_client.py` by `mutant_bootstrap.py` and ran the selected `test_loaded_ordered_shape_override_reports_probe_failure` case with `-k`.

| Mutation | Case | Actual summary and failure |
|---|---|---|
| Skip validation with zero allowance | `6-True` | `1 failed, 131 deselected in 1.68s`, wrong `PROBE_INCOMPLETE` |
| Propagate the raw preparation exception | `8-True` | `1 failed, 131 deselected in 1.58s`, raw `ValueError` |
| Drop the second failed case reason | `8-False` | `1 failed, 131 deselected in 1.46s`, second case reports `PROBE_INCOMPLETE` |
| Skip caching the failed attempt | `8-True` | `1 failed, 131 deselected in 1.70s`, `ordered_events=2 attached=6` |

All four exited 1 at the target assertion. The scratch matcher's last expected failure signature was set to the later cache identity check. The earlier duplicate-event assertion caught that mutant and was confirmed from the captured output. **4/4 killed**, no survivors or collection failures.

#### Direct K2 caller checks

The same limiter and flags ran `test_k2_probe.py`, `test_k2_fix_round2.py` and `test_k2_fix_round3.py` one per process (`run_checks.py files test_k2_probe test_k2_fix_round2 test_k2_fix_round3`). Initial summaries were `4 failed, 14 passed in 1.12s`, `1 failed, 41 passed in 21.16s` and `35 passed in 6.02s`. The first two exposed stale test expectations for the approved C2/C3 protocol. The injected refusal double now supplies a nullable string and the injected timeout test verifies both closed case reasons on its sanitized event and the aggregate in the proof. Those two files reran with `18 passed in 0.99s` and `42 passed in 20.75s`, exit 0. No runtime edit followed the ten-file run.

#### Static checks and handoff

- **Changed files.** `model_client.py`, `model_probe.py`, `tests/test_probe_request_settings.py`, `tests/test_k2_probe.py`, `tests/test_k2_fix_round2.py` and this task file. Work remains uncommitted for PM integration.
- **Static checks.** Scoped `git diff --check` returned exit 0 with empty output. `PY -B -I -m ruff check <changed Python paths>` and `PY -B -I -m ruff format --check <changed Python paths>` each exited 1 with `<project>\.venv\Scripts\python.exe: No module named ruff`. Ruff lint and format are `not_run`.
- **Other `not_run`.** Full suite and CI are PM integration checks. Live model, GPU, cloud, deployment performance and installation were outside this phase. The smoke send budget and provider behavior were checked using labelled doubles.
- **Cleanup.** All child processes exited. The fixed basetemp and the scratch mutant were absent before cleanup, and `Test-Path -LiteralPath .tmp/KBS-F2-fix` returned `False` afterwards. The slot directory had no lock files. The task file retains the command results.
- **Status.** `REVIEWING`. REV2-01 has a passing red/green reproduction across all three variants. The PM can re-review the uncommitted patch with the evidence above.

## Fix round 3

### Step 0

R-015, R-018. Small test-only task. REV3-01 comes from `.tmp/KBS-F2-rev3/review-codex.md`. The ordered probe normalizes both a deadline and a failed response inspection to `PROBE_FAILED`, and the old double could complete with `None` if its sleep was allowed to finish.

**Which independent observation distinguishes the causes?** Record and re-raise `asyncio.CancelledError` inside each transport attempt. Assert both attempt identities were cancelled and that the real pair dispatcher received the configured `timeout_s`. Keep the closed proof/event assertions and the two-send check. The source reaches `asyncio.wait_for` through `model_client.py:504–511` and `model_probe.py:324–345,363–370`. Using cancellation avoids a separate wall-clock threshold.

**What else can a closed assertion hide?** The direct K2 refusal test checks request counts and order-sensitive proof status. Its negative path needs the exact refusal code because a malformed refusal is also negative. The request-settings tests use explicit HTTP/finish/provider replies, actual send counts where admission is under test, and durable diagnostic assertions. Keep those witnesses and check them in the final sweep.

May assume existing profile values, runtime deadlines, closed codes and probe accounting. New runtime policy, schema and profile changes are outside this round. Configuration impact is limited to observing the existing test override for `vlm.backends.*.probe.timeout_s`; live configuration and examples retain their values. The local recall build returned `indexed=94 unchanged=0 removed=0 refused=0 chunks=689 skipped_chunks=0`. `probe timeout` returned two unrelated lock-timeout sections.

1. Replay the reviewer deadline mutation and a refusal classification mutation on scratch copies with the original tests.
2. Add the independent cancellation and timeout checks, preserve closed events, and pin the explicit refusal result. Rerun both mutants and the three assigned files serially through the limiter.
3. Record the sweep and exact results. Use an in-process transport adapter for the two socket cases under the no-server limit and remove scratch after verification.

### Steps

- **Step 1 complete.** Before the test edits, the deadline and refusal mutants survived their targeted one-case runs.
  The refusals reached the same negative status, and the longer deadline still yielded the same closed aggregate.
- **Step 2 complete.** The deadline test now observes the forwarded override and both cancellations. The explicit refusal asserts the aggregate and both case reasons.
  Both scratch mutants failed at the new assertions. All 192 assigned cases passed serially, including two adapted socket cases.
- **Step 3 complete.** The configuration audit records the independent timeout witness. The class sweep, commands and exact results are below.
  Every child exited and the single basetemp and task-owned scratch were removed.

### Completion report for phase 5

**REV3-01 cause and fix.** `test_lazy_proof_uses_configured_deadline_without_retrying_unknown_result` now records each transport attempt and, inside its `CancelledError` handler, records the attempt's cancellation before re-raising. It also observes `0.01` seconds entering the real ordered-pair dispatcher. Two cancellations, two attempts, and one dispatcher invocation prove both the deadline behavior and the cached failure on repeated demand. Closed `PROBE_FAILED` assertions remain on the aggregate and both sanitized cases. Python 3.12's installed `asyncio/tasks.py:472,519–520` shows the positive-timeout `wait_for` path wrapping the await in a timeout context.

| Swept test area | Independent observation |
|---|---|
| `test_k2_fix_round2.py` | Configured timeout at dispatch, identities of both cancelled attempts, exact send count and no repeated probe. |
| `test_k2_probe.py` | Order-sensitive responses, exact send count, first/second case status and partial-allowance cutoff. Explicit refusal now requires `PROBE_REFUSED` on the aggregate and both cases. The added mutation proves a malformed refusal would previously satisfy the negative assertions. |
| `test_probe_request_settings.py` | Truncation observes no generation and verifies received role controls. Mixed/partial groups and malformed preparation check received send shapes and counts independently. Invalid preparation also verifies remaining allowance and reuses the same cached proof. The reply and checkpoint tests specify exact input envelopes, verify persistence and ensure reply text stays out of diagnostic evidence. No additional test edit was required. |

Both runtime files used for scratch mutations had identical pre/post SHA-256 values. This round changes test assertions and the configuration audit evidence only. No operational value or literal changed. Loader refusal evidence remains in the audit from the previous round.

| Scratch mutation | Before | After |
|---|---|---|
| Dispatch with `max(timeout, 2)` | `1 passed, 41 deselected in 3.37s` | `1 failed, 41 deselected in 3.50s` at `assert cancelled_attempts == attempts`, actual `[] == [0, 1]` |
| Report a valid refusal as malformed | `1 passed, 17 deselected in 0.29s` | `1 failed, 17 deselected in 0.42s` at the aggregate assertion, actual `PROBE_MALFORMED` versus `PROBE_REFUSED` |

#### Commands and final results

`PY` is `<project>/.venv/Scripts/python.exe`. The scratch launcher invoked the real slot limiter with `runpy`, adding `CREATE_NO_WINDOW`, a 240-second child deadline, output capture and basetemp cleanup. Children used `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src` and task-local Hypothesis storage. `BASE` is the absolute `<worktree>/.tmp/KBS-F2-fix/basetemp`.

```text
PY -B -X faulthandler .tmp/KBS-F2-fix/check_round3.py before|after|suite
tools/agent-workflow/test_slot.py -- PY -B -X faulthandler -m pytest -q --tb=short -p no:cacheprovider -o faulthandler_timeout=60 --basetemp=BASE tests/<file>.py
```

For each mutation, `pytest_bootstrap.py mutant <module> <scratch>/mutant.py` replaced `-m pytest` and added `-k lazy_proof_uses_configured_deadline` or `-k "multi_image_probe_requires_both_images_and_order and False-True-2-False"`. Each mutant was a scratch copy of one module. Both before runs exited 0 and both after runs exited 1 at the intended assertions. No collection failures.

| File | Actual output | Exit |
|---|---|---|
| `test_k2_fix_round2.py` | `42 passed in 21.79s` | 0 |
| `test_k2_probe.py` | `18 passed in 1.14s` | 0 |
| `test_probe_request_settings.py` | `132 passed in 18.01s` | 0 |

For `test_probe_request_settings.py`, `pytest_bootstrap.py no-server` replaced `-m pytest` and adapted only the two loopback cases to in-process `httpx.MockTransport`. The existing doubles still answer from the actual request path and JSON. Evidence class is CPU unit and mock contract. **192 passed** across three process runs, with **190 unadapted and two adapted** cases. The socket versions of those two cases are `not_run` under the no-server limit.

#### Handoff

- **Status.** `REVIEWING`. Changed `tests/test_k2_fix_round2.py`, `tests/test_k2_probe.py` and this task file. Independent cancellation and typed refusal checks close the test-oracle gaps without changing the accepted runtime behavior. The PM can re-review the uncommitted patch.
- **Static checks.** Scoped `git diff --check` exited 0. `PY -B -I -m ruff check` and `PY -B -I -m ruff format --check` over both changed tests each exited 1 with `<project>\.venv\Scripts\python.exe: No module named ruff`. Ruff lint and formatting are `not_run`, as are the full suite, CI and live checks.
- **Cleanup.** Every run printed `BASETEMP_EXISTS=False`. `Test-Path -LiteralPath .tmp/KBS-F2-fix` returned `False` after removal and the test-slot directory had no locks.
- **Lesson.** Preserve a direct behavioral witness when replacing raw causes with closed diagnostics. The mapping can make distinct causes observationally equivalent at the public boundary.
