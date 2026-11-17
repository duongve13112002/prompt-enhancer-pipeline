# KBS-F1: production capability probes for model stages

## Fix round 1: Step 0 (Small: yes; proceed)

**Status: REVIEWING.** Fix round 1 focused verification is complete; Claude retest is next. Scope: R-015, R-018, R-020, R-026; no new setting, inference, external traffic, Git write or flow-file run. **May assume:** approved strict verifier scope, existing fail/fallback policy, run-scoped credential lifetime, labelled CPU doubles. **Must ask:** new configuration or spend/proof semantics; none needed. **PM decision pending user review:** follow the existing allowance-to-incomplete result conversion; clear/rethrow any escaping refusal/cancellation before evidence exists, preserving already observed proof and spent sends.

### Research evidence and diagnosis

Read raw `.tmp/KBS-F1/review.md` F1-F4 and its scratch repro. Source-verified: `ModelClientRun.for_role` swallows cloud `BudgetExhausted` only for configured fallback, but `_production_probe` leaves its pre-await mark without proof; re-entry then skips the capability. `_call` also has a later lazy-proof path outside its startup budget handler. Both must reach the existing policy. `probe_capabilities` already converts allowance refusal into an incomplete result and transport/timeout into failed results; cancellation escapes. `ModelTransport.post` reserves before allowance debit, releases an unsent reservation, and commits before a possible send. Preserve that conservative actual-send accounting.

Credential sweep source-verified: all three `cloud_headers` call sites are in `model_client.py` (production probes, injected startup, generation); `model_cloud.cloud_headers` alone calls `load_api_key`. Cache headers lazily per backend in the run-owned RoleClient; clear the cache when the run closes. No agent reads credential storage. F4 is documentation only: strict matching demonstrates instruction following, not server response_format enforcement. Primary sources: `model_client.py`, `model_probe.py`, `model_transport.py`, `model_resources.py`, `model_cloud.py`, `docs/rules/VLM_CALL_RULES.md` rules 2/9/11. No new external algorithm/API behavior. Recall build: `indexed=0 unchanged=94 removed=0 refused=0 chunks=0 skipped_chunks=0`; search `probe cloud budget exhaustion fallback credentials` returned no hits.

### Configuration impact (before runtime edits)

| Value / behavior | Existing schema paths and profiles | Planned validation / evidence | Class |
|---|---|---|---|
| Cloud fail/fallback and grant | `enhancer_config.v0`, `vlm.backends.*.{on_budget_exhausted,call_budget}`, `vlm.default_backend`; base/dev examples unchanged | Loaded fail/fallback and grant 0/1/2/3 effects through production proof; missing/sentinel/invalid refusal; initial and lazy budget exhaustion | configurable |
| Probe cap/deadline and retries | Same schema, `vlm.backends.*.probe.*`, `.http.*` | Preserve configured send cap and timeout/retry behavior; sweep allowance, HTTP error, timeout, cancellation; no cap increase | configurable; debit safety-bound |
| Credential headers/lifetime | Same schema, existing `vlm.backends.*.{provider,api_key_file,anthropic_version,anthropic_workspace_id}` | One load per used backend per run across startup/extensions/generation; synthetic loader counter, no filesystem credential fixture; cache cleared on close | configurable; memory ownership safety-bound |
| Evidence / verifier scope | Closed codes, existing anchored checkpoint contract, no profile key | K3/K4 persisted failed probes; no negative proof from unsent refusal; document instruction-following limit | protocol-bound / safety-bound |

### Plan and finish

1. **Done:** added a red production cloud-fallback regression, exception/policy properties, K3/K4 durable evidence tests and credential read-count checks.
2. **Done:** fixed pre-evidence marks and initial/lazy exhaustion policy, cached run-owned headers and documented F4. Completed proof and actual-send debits remain intact.
3. **Done:** requested focused files passed serially, one fixed basetemp per file; no flow/full suite. Six changed-line mutants killed on module-only scratch copies. Ruff remains not_run because unavailable.
4. **Done:** exception/credential sweep, configuration audit and actual command/output report completed; owned scratch removed, stop for Claude retest. Final production-proof file: 75 passed.

**Step 1 evidence:** unchanged-round production HTTP regression reproduced F1: `1 failed, 53 deselected in 2.34s`, `CAPABILITY_UNAVAILABLE: required capability is unproved`; `.tmp/KBS-F1/fix1-red.log`. Its tree was removed after exit. No runtime edit preceded this red result.
**Step 2 evidence:** production proof subsequently passed `73 passed in 27.67s`, real CLI `1 passed in 3.64s`, client `78 passed in 11.00s`, composition `54 passed in 61.32s`; K3 `22 passed in 39.76s`, K4 `44 passed in 26.01s`. Final production-proof rerun passed 75 tests, including escaping-refusal cases and one complete credential-count test.

### Exception and credential class sweep (fix round 1)

| Boundary / exception | Attempt mark and proof | Result / policy | Reservation | Evidence |
|---|---|---|---|---|
| Cloud ledger `BudgetExhausted`, initial or lazy ordinary probe | Pop current capability mark, rethrow; preserve earlier completed proof and spent allowance; do not set a new proof | Public bind/call uses existing `stop` or `fallback_to_default_backend`; stop is `BUDGET_EXHAUSTED`, fallback warns and proves local backend | No reservation when reserve refuses; earlier committed sends stay spent | Production loopback regression and `test_production_budget_policy_property` (grants 0/1/2/3, ordinary/lazy/generation) |
| Actual-send `ProbeLimitExhausted` | Normal helper conversion records incomplete evidence and retains that completed attempt status; an escaping raw exception pops the mark and rethrows | `PROBE_INCOMPLETE`; no cloud-budget fallback because this is the independent probe cap; no negative proof | Any uncommitted cloud reservation released before rethrow; allowance-refused send not charged | Exception property, existing retry/send-cap tests, `test_escaping_probe_allowance_remains_incomplete` at bind/call |
| `httpx.TransportError` or unsuccessful HTTP response | Helper records failed evidence; retain failed mark so retries/later binds cannot reset the run cap | `PROBE_FAILED`; no budget fallback unless a configured retry itself exhausts the cloud grant | Sent attempts committed, no active reservation after transport exit | Exception property and production retry tests |
| `asyncio.CancelledError` escaping ordinary or ordered proof | Pop current ordinary mark / discard ordered mark, propagate cancellation; retain completed proof and debit | Cancellation propagates, never `CAPABILITY_UNAVAILABLE`; a later bind can retry using remaining allowance | Before allowance admission, release reservation; once commit/send begins, preserve the attempted-send debit | Exception property, `test_cancelled_ordered_probe_does_not_poison_extension`, unsent reservation test |
| Probe deadline timeout | Existing helper records failed evidence and keeps failed mark | `PROBE_FAILED`; no negative proof or budget fallback | Timeout after attempted send stays spent; no active reservation | Exception property (timeout only uses short deadline), configured timeout effect test |
| Request mapping `ValueError` / `TypeError` / `KeyError` before send | Existing bounded failed mapping event is recorded; retain mark, no false capability inserted | Existing `CAPABILITY_UNAVAILABLE` for unproved provider mapping; no budget fallback. This is mapping refusal, not observed negative model evidence | No reservation/send | Source-verified production mapping guard; existing image/output-combination tests |
| Other escaping exception before ordinary/ordered evidence | Clear the in-flight mark and rethrow; no fabricated proof | Existing public structural classifier applies; no policy inferred | Transport releases a reservation on any pre-admission BaseException; committed requests remain spent | Source sweep of guarded send/extension and RuntimeError unsent-reservation case |
| Synchronous request mapping refusal | Existing failed event recorded; no send or false capability; retain refusal | Existing mapping refusal `CAPABILITY_UNAVAILABLE`, distinct from grant/allowance refusal; not negative observed evidence | None | Existing unsupported-mapping tests; mapping semantics unchanged in this fix |
| Production probe headers (`_production_probe`) | `_headers(backend_name)` inside demand loop after skip; cached by backend in run-owned RoleClient | Load lazily once for each used cloud backend/run; no credentials in records | Not applicable | Header-count test across binds, generation, lazy image proof and a fresh run |
| Injected startup headers (`_start_backend`) | Same cache instead of direct `cloud_headers` | Existing ledger/project checks retained | Not applicable | Composition/client cloud tests; common helper source-verified |
| Generation headers (`_call`) | Same cache instead of direct `cloud_headers`; run.close clears it in finally | Local headers remain empty; fresh run loads again | Not applicable | Header-count test; client/composition tests |
| Credential loader (`model_cloud.cloud_headers` -> `load_api_key`) | Single source of credential reads; callers now share cached headers | Only project Python reads credential storage; no agent secret access, no logging | Not applicable | Scoped source search found exactly these three client call sites; function left unchanged |

**Explained first property-run failure:** `1 failed, 67 passed in 51.47s`. Its per-file log was overwritten by the subsequent serial run; the actual tool-output summary and two diagnostics are transcribed in `fix1-property-first-summary.txt`, not presented as a retained full traceback. Every fault used a 0.01-second deadline, allowing a timeout before the intended allowance/cancellation boundary. The first output showed `PROBE_FAILED` for those variants. Only test deadlines changed: non-timeout faults now use configured 2 seconds, timeout still uses 0.01. No runtime verifier, allowance arithmetic or fallback policy was relaxed. The next full file passed. F4 scope: text/JSON proofs demonstrate instruction following; `response_format` enforcement and constrained decoding remain unproved, as now stated in rule 2 of `VLM_CALL_RULES.md`.

## Brief header

- **Kind / role:** code; implementer; Codex worker. Step 0 and verification continuations approved. Focused checks complete; ready for independent review, with Ruff not_run as instructed.
- **R-IDs:** R-015 (explicit configuration and structural checks), R-017 (resume and identity), R-018 (configured HTTP providers and budgets), R-020 (CPU plumbing evidence, no quality claim), R-026 (untrusted data and safe diagnostics).
- **Goal:** make production model-client construction supply verifiable capability probes without test injection, and preserve safe structural failure reasons through K2/K3/K4 and `kb sync`.
- **Original Step 0 done check (completed before approval):** this task file contains the evidence, source decision, artifact checks, request accounting, complete caller/failure sweep, Configuration impact, ranked questions and implementation/verification plan; no runtime edit or execution of that plan at Step 0.
- **Implementation done check after approval:** a new CPU regression fails on the current implementation and passes through the actual `kb sync` entry point without injected probes; required focused files, properties, configuration override/refusal tests, guards, lint/format and scratch mutants are reported with actual output.
- **Scope now:** approved implementation scope below, based on `.agents/policy/AGENT_TASK_TEMPLATE.md` and `.tmp/KBS-F1/stub.md`.
- **Proposed scope after approval:** `model_client.py`, `model_probe.py`, the four K2/K3/K4 stage modules, `kb_sync.py`; `model_transport.py` only for approved Q3 accounting; `profiles.py` only for the swept typed-code consumer; a small versioned probe bundle under `contracts/probes/`; focused pipeline tests and labelled HTTP-double fixtures. Configuration/schema/example edits only if the approved source decision requires new settings. All source paths are under `src/prompt_enhancer/`.
- **Out of scope:** T-014 implementation, server management, model quality, embeddings implementation, full-suite execution, unrelated carried findings, PM-owned status/decision files, Git writes, network/downloads/installs, live model/GPU work, writes to `database/`, `runs/` or `models/`, and any secret access.
- **Output:** English reply under 12 lines; details here. PM arranges the independent Claude review; this worker does not create sub-agents or review its own patch.
- **Step budget / run limit:** Step 0 is a bounded source audit and one document. Implementation is a separate approved dispatch; one test process at a time, one file per pytest process, no `-n`, no full suite.
- **May assume:** existing profile/provider choices, immutable challenge answers as artifact-bound constants, labelled CPU doubles, existing ownership of injected clients, conservative refusal of unproved capabilities. Routine reversible choices are recorded here as **PM decision pending user review**.
- **Must ask:** ordinary probe source (not settled in reviewed contracts), any new deployment/configuration field, changes to proof semantics or request-budget meaning, and any loopback exception to the no-network limit. No live spend is requested.
- **Test sets:** pipeline suite only: every `tests/test_kb_sync*.py`, `tests/test_k2_probe.py`, `tests/test_model_client.py`, changed K2/K3/K4 test files; `tests/test_field_contracts.py`, `tests/test_storage_anchored_writes.py`; new production-entry regression; probe artifact/failure-code properties; per-key configuration overrides and missing/sentinel refusals. Never combine `tests/` and `tools/agent-workflow/tests/`.
- **Trivial / Small:** no / no.
- **Base:** `637611f6aa111c5cf9a75e4f0c5db16a6e765982` (`git rev-parse HEAD`). No Git write performed. Initial unscoped status reported a missing protected README; no secret path was opened or inspected, and subsequent Git checks must be explicitly scoped.

## Step 0 (before any runtime edit)

### Unknowns and failure modes

1. Ordinary startup probe *source* is absent from the reviewed contracts. They demand independently verified replies, not guessed capabilities. Blindly replacing `{}` with permissive booleans would hide the defect.
2. Local probe plans currently yield `image` and `reasoning.mode`, but not ordinary `json_object`/`json_schema` proof (`model_client.py:250-273`). Supplying only that plan would move the failure to required-output validation at `:370-372`.
3. A shared backend can bind a text role before an image role. Later demands must extend proof without resetting its allowance, losing previous evidence, or reattempting known negative/failed proof. Separate per-stage runs also reset cloud ledgers; the coordinator should own one lazy run for its model stages.
4. HTTP success, accepted sampling fields, or absence of reasoning text alone cannot establish that a backend obeyed a setting. Unsupported/unproved optional controls must remain unknown/negative with the existing warning/drop policy. Required image/output capabilities still refuse.
5. A damaged, swapped or escaped probe artifact must refuse before requests. Bundle identity is distinct from backend/model/role configuration and from the exact wire-body hash; do not confuse self-computed hashes with pinned integrity checks.
6. Probe limits currently count case reservations; transport retries can multiply wire requests. A precise accounting decision is needed before changing that behavior (Q3). Incomplete and failed probes must remain distinct, including a limit exhausted partway through a pair.
7. Safe diagnostics must distinguish artifact/setup failures, missing/negative proof, refused/truncated generation, HTTP errors and exhausted budgets, while never echoing model text, endpoint credentials or exception messages.
8. The brief requires an actual local HTTP double but also says no network. A loopback-only CPU test exception needs explicit approval (Q4); no listener is started in Step 0. Windows and Linux tests must use bounded waits and release their listeners/threads; child processes, if needed, use `CREATE_NO_WINDOW` on Windows and `-X faulthandler`.

### Research required before code / evidence

Paths in the smoke rows are relative to the main project root, supplied read-only in the dispatch; other paths are relative to this worktree. No machine path is embedded here.

| Question | Source (version / location) | Read raw or summary | Finding | Class |
|---|---|---|---|---|
| What actually failed? | Main-project `.tmp/E-kbsync/plan.md:128-135`; `runs/E-kbsync-20261117-113247/smoke/driver_events.log:9-13`; `sync.stderr.txt:1` | Raw | Driver records sync exit 1; stderr is exactly `k2_naming: STAGE_FAILED: count=1`. Report records failed `k2_assessment`, `MODEL_CALL_FAILED`, empty call records. Checkpoint contents were not independently reread in this task. | documented smoke report; source-verified log/stderr |
| What did server stderr show? | Same smoke directory, `llama.stderr.txt:1-10`, `embedder.stderr.txt:1-21` | Raw | Startup/weight-loading evidence; neither stderr supplies missing production probe definitions. No new model run performed. | source-verified |
| Where is the wiring gap? | `kb_sync.py:139-157`; `k2_naming.py:522-523`; `k2_observe.py:269-270`; `k3_cards.py:283-284`; `k4_aliases.py:184-185`; `model_client.py:274-275` | Raw | Coordinator passes neither map; each stage coerces absent maps to empty maps; client raises before a request. | source-verified |
| What must production prove? | `docs/rules/VLM_CALL_RULES.md:5-15`; `docs/dev-machine.md:33-43`; `contracts/vlm-request.md:9-15,61-65` | Raw | Startup requests and recorded results; image, structured-output and thinking combinations; no implicit request settings; validation after the engine; labelled doubles for CPU verification. These sections specify no ordinary fixture file/path or source. | documented |
| Which production source is already decided? | `docs/stages/K2-observe.md:41`; `model_probe.py:143-163`; `tasks/active/T-010-k2-k3.md:123,155` | Raw | Built-in PNGs of one/two black circles; ordered answers 12/21; requesting role's backend model/token cap; lazy `multiple_images` demand and shared allowance. Keep this established behavior. | documented / source-verified |
| What counts as proof? | `model_probe.py:23-69,72-140`; `tests/test_k2_probe.py:42-56,125-149` | Raw | Strict bool verifier, incomplete versus failed, paired image-dependent answers; image/thinking pair validators; existing tests explicitly use doubles and injected ordinary plans. HTTP 200 alone is insufficient. | source-verified |
| Are budgets shared and retries counted? | `model_client.py:82-99,192-240,249-289`; `model_transport.py:45-70`; `tasks/active/T-009-model-client.md:17,23,86,105,378,406` | Raw | One run owns concurrency/ledger; initial and lazy probes share `_probe_remaining`; transport retries and every cloud attempt reserve the ledger. Current case limit is not a hard bound on retried HTTP attempts. Separate independently composed runs do not share a ledger. | source-verified / documented |
| How are optional fields handled? | `model_mapping.py:18-39`; `model_client.py:407-419`; `docs/rules/VLM_CALL_RULES.md:9` | Raw | Unproved sampling/reasoning fields are dropped with `PARAM_UNSUPPORTED`; do not turn request acceptance into behavioral proof. | source-verified / documented |
| Do production probes imply server lifecycle? | `docs/rules/VLM_CALL_RULES.md:5`; `kb_sync.py:84-88,136-160`; main-project `.tmp/E-kbsync/plan.md:55-58` | Raw | Binding rule: "The repository never installs or starts a production server." Deployment supplies HTTP servers; the coordinator does not launch or stop them. No server orchestration change. | documented / source-verified |
| What is already authorized? | `tasks/DECISIONS_INDEX.md:59,64` (decisions 128, 133); `tasks/STATUS.md:18-30`; `tasks/LESSONS_LEARNED.md:5-19,23-78` | Raw | Decision 128 accepts earlier pending choices and the dev image-token setting; 133 authorizes PM dev artifact/sentinel work. Neither selects an ordinary probe bundle or grants this worker model/GPU/network activity. Read the rule digest and relevant recent entries. | documented |
| What is currently configurable? | `contracts/schemas/enhancer_config.v0.schema.json:3639-3646`; `config.py:1263-1274`; base example `:199-200`; dev example `:67-68` | Raw | `probe` has exactly required `max_requests` and `timeout_s`, additional properties forbidden; examples retain design sentinels. Source-path configuration would be a new decision. | source-verified |
| Why did flow tests miss this? | `tests/kb_sync_fixtures.py:47-56`; `tests/test_kb_sync_flow.py:18-39`; `tests/test_k2_probe.py:60-100` | Raw | Flow fixture injects a bound client and embedding transport; multi-image test injects ordinary local plans. Neither tests unassisted production composition. | source-verified |

Searches: scoped source search of `src/prompt_enhancer/*.py` for constructors/composition and stage calls; scoped docs/contracts/prompts/config searches for probe artifacts/fixtures/manifests returned no source specification. `recall.py search 'production capability probe artifacts image thinking' --limit 5` was attempted with project venv and required environment; exit 1, exact diagnostic after the prefix: `no database at <worktree>/.tmp/pm-state/recall.sqlite: run `recall.py build` first`. No shared recall state was created/changed; direct decisions/status/source searches supplied the evidence. T-014 has no matching active task file in this checkout; its current in-flight implementation was not inspected or edited.

### Ranked questions (PM decision pending user review)

1. **Ordinary probe source:** approve a small checked-in, versioned synthetic bundle at `contracts/probes/model_capabilities.v0.json`, with code-pinned identity/size/SHA-256 and embedded tiny image fixtures, composed using existing validated backend/role configuration? **Proposed answer: yes.** Reviewed docs leave this source undecided; this avoids new user-selected paths or deployment keys. Built-in generated ordinary challenges are an alternative, but a single bundle makes identity/hash corruption testable without adding a configurable manifest system. Preserve the already documented generated multi-image pair.
2. **Proof boundary and provider scope:** approve production text/JSON/image probes through existing provider adapters, with strict deterministic verifiers; optional sampling/thinking fields stay unknown and warned unless independently observable evidence proves them? **Proposed answer: yes.** No unconditional true verifier, hard-coded provider capability table, or HTTP-acceptance proof. Preserve Gemini's existing unverified-image refusal. A cloud role/fallback must obey its configured `allow_images`, user grant and shared ledger; no cloud call will be run by this worker. Any missing engine-specific reasoning evidence is reported as unproved, not invented.
3. **Probe request accounting:** should `probe.max_requests` bound actual HTTP attempts, including retries, across initial probes and every lazy extension for one backend/run? **Proposed answer: yes.** Existing code bounds logical cases while retries can multiply requests. Keep existing HTTP retry settings, count before each attempted send, and distinguish allowance exhaustion from negative evidence. No hidden automatic cap increase and no concrete new default. Example: cap 1 cannot complete a two-request image pair and must refuse image use; cap 2 with one retry spent cannot send a third request.
4. **CPU loopback exception:** authorize only a bounded local HTTP double for the implementation tests, despite the general no-network limit? **Proposed answer: yes, loopback only, no external traffic or inference.** Use an OS-assigned port and owned server thread in tests, with all ordinary model/embedding requests going through real HTTP transports; no injected probe maps/results/client or patched probe functions in the acceptance regression. Step 0 starts no server.

### Proposed probe source, composition and integrity

**Historical Step 0 proposal; Q1-Q4 were subsequently approved in the implementation dispatch below.** No reviewed document specified pre-existing ordinary probe cases. The final Completion report supersedes the earlier plan-only status.

- The fixed versioned bundle contains challenge identifiers, strict tiny reply schemas/expected structural answers and two tiny synthetic PNGs. No knowledge-base image or user text enters a probe. Bundle fields do not carry model IDs, endpoints, request/token budgets, inference settings or fitted thresholds.
- Load the fixed artifact only after resolving the project root and confirming the final path stays inside it (including links). Check the exact expected version/identity, byte length and SHA-256 against independent pinned values in code before JSON parsing/composition; refuse missing/unreadable/malformed/unknown/extra fields as a safe probe-artifact code. Validate each embedded image's decoded length/hash and declared format/dimensions against the bundle's pinned declarations. The production multi-image generator retains its current structural geometry and answers; record its emitted image hashes as provenance, not as a self-certified external artifact.
- Property tests vary identity, path containment, length, bytes/digest, base64, shape, duplicate/mismatched case identifiers and strict integer answers; every corrupt/mismatched bundle must refuse before any transport request. Valid equivalent artifacts must retain their identity. Never pass merely because a digest recomputed from untrusted content matches another value recomputed from that same content.
- Compose request bodies using the selected backend's provider, model ID, engine identity and endpoint plus the requesting role's configured token cap, applicable generation/output settings and image policy. Avoid bootstrapping by calling the probe-dependent public role client; use the existing low-level transport/provider request builders. Unknown capability-controlled settings are not silently claimed supported; preserve warning/drop provenance. Apply configured engine extras deliberately and include the resulting exact body identity in probe evidence.
- Generic composition supplies ordinary `probe_cases`/local plans when the optional injection arguments are absent. Preserve explicit injected maps as labelled-test seams; an explicitly empty injection must not silently become fabricated evidence. Do not force the mutually exclusive current `local_probe_plans` branch to suppress ordinary output proof: combine verified results while retaining their independent status/reason.
- Start from demanded text/output/image capabilities. A text-only first role must not send an image probe to a backend that forbids images; a later role can extend evidence once within the same allowance. Cache failures/incomplete attempts and preserve existing proof. The existing two-image pair stays lazy and role-bound as required by K2's documented contract.
- Store bounded probe evidence through existing run/call/checkpoint reporting: challenge/bundle ID and hashes, backend/provider/engine/model revision, requested capability, observed bool/unknown, status, safe reason, attempted count and body hash. No new standalone trace-storage subsystem. Raw model text never enters operator error messages.

### Request budget and verification costs

Approved schedule costs per role on a fresh backend, with no retries and the role's demanded structured format: `info_assessor` 2 (text 1 + format 1); `folder_namer` 6 for one image (text 1 + format 1 + image 2 + combination 2), 8 when multiple images are demanded (ordered pair +2); `observer` 6; `card_builder` 2; `alias_generator` 2. Already verified capabilities are reused: assessment then multi-image naming then observation/cards/aliases on the same backend needs 8 total sends, assuming one common output format and no retries. Different demanded formats add their own output/combination cases. CPU labelled-double tests verify the schedule and standalone stages; this is no claim about a live model. Optional thinking/sampling production challenges cost 0 until independent observation is available.

Current source: `probe_backend` uses one case per request (`model_probe.py:55-69`); image/thinking/multiple-image challenges each need two requests (`:106-110,136-140,199-203`). `_take_probe_requests` reserves from one backend allowance (`model_client.py:204-207`). The smoke selected `max_requests: 1` (main-project `.tmp/E-kbsync/plan.md:24`); that is insufficient for any paired proof and must not be silently raised by this fix.

Proposed ordinary schedule, demanded lazily: text challenge 1; JSON-object challenge 1 when that format is needed; JSON-schema challenge 1 when needed; single-image dependency pair 2 for image input; image-plus-selected-JSON-format pair 2 where that combination is required. The last pair records combination evidence rather than inferring it from independent successes. A fully requested text + both JSON formats + image + one image/JSON combination therefore needs 7 attempts without retries; this is schedule arithmetic, not a recommended profile default. A verified thinking on/off pair would add 2 only where an independently documented observer is available; otherwise no positive proof is issued. Existing lazy ordered multi-image proof adds 2. Optional field challenges must be listed and charged before adding them; no semantic sampling test or tolerance is invented.

Q3 is implemented as one cap on actual sends, including retries and failed/cancelled attempted requests, shared across roles and extensions; `probe.timeout_s` bounds each challenge, and configured HTTP timeouts remain effective. Cloud attempts additionally debit the existing backend `call_budget`; no grant is created, no budget is reset by binding a role, and fallback probes spend the effective backend's own allowance. Unlike the baseline logical-case reservation, retries cannot multiply the actual-send cap. A send refused by probe allowance releases its uncommitted cloud reservation. Incomplete due to insufficient allowance, transport/verification failure, negative proof and unsupported provider mapping remain distinct. A run with no pending model work performs no probes.

### Class sweep: production construction and entry points

Implementation dispositions: common constructor/composition uses the pinned production bundle; all four standalone stages preserve absent injection maps; coordinator owns one lazy shared run; the classifier/summary and profiles consumer preserve closed structural codes. T-014 remains handoff-only, with S2 requirements below and no edits to its files. The tables describe baseline defects and their dispositions; final executed evidence belongs to the Completion report.

| Site | Baseline behavior | Disposition / verification |
|---|---|---|
| `__main__.py:15-46` -> `k0_sync.py:119-125` | Only implemented CLI model-stage route is `kb sync`; loads real profile, calls coordinator, prints final JSON or exception. | Acceptance test calls `main(['kb', 'sync', '--config', ...])` or the module entry; no probe injection, no bypass of loader/coordinator. Verify successful publication, safe diagnostic failure and an unchanged repeat with zero new HTTP calls. No additional standalone K2/K3/K4 CLI command exists in this file. |
| `kb_sync.py:84-88,119-157` | Optional caller-owned client; otherwise every model stage owns a separate run with no cases. `--plan`, retrieval-off and unchanged branches avoid model work. | After unchanged/plan guards, own one lazy `ModelClientRun` when no client is supplied; pass its role-binding interface to all model stages and close in `finally`. Preserve caller-owned injection and one writer. Shared probes/ledger across the full sync; no eager probe on a no-work run. |
| `k2_naming.py:447-448,522-523` | Standalone async stage constructs run with `probe_cases or {}` and `local_probe_plans or {}`. | Preserve absence for production factory; retain explicit test injection. Verify assessment plus single/multiple-image naming, own-only close, no unnecessary image request. |
| `k2_observe.py:156-158,269-270` | Same empty-map construction for observer. | Same production composition; require image plus selected output evidence; standalone and coordinator paths, pending/failed/done checkpoint invariants. |
| `k3_cards.py:180-181,283-284` | Same empty-map construction for card builder. | Same production composition; text/output proof; standalone and shared-run behavior. |
| `k4_aliases.py:128-129,184-185` | Same empty-map construction for alias generator. | Same production composition; text/output proof; standalone and shared-run behavior. |
| `model_client.py:82-99,151-158` | Constructor and `compose_model_client` require injected maps; composition closes on failure. No production caller of `compose_model_client` found in current `src/`. | Make absent maps resolve to real production definitions at the common boundary; retain explicit injection, lazy backend start and close-on-bind-failure. Direct composition regression without injected probes. |
| `model_client.py:179-289,324-368` | RoleClient consumes maps; only multiple-image plan generated; provider/image/budget fallback starts effective backend separately. | One production source at common boundary, demand extensions under same lock/cap, fallback uses effective backend identity. No false proof on missing/incomplete/negative evidence. Retain explicit low-level RoleClient test construction. |
| T-014 S2 request path (no current constructor/composition caller in this checkout) | In-flight implementation not present here; no T-014 file edited. | PM handoff: use production factory without test probes, share a request-owned run across planner/live-observer roles, pass exact single/multiple-image demands, preserve manifest resolution where relevant, persist safe probe/call evidence and close the owned run. No dynamic grant or fabricated evidence. T-014 owner adds its own request-path acceptance test. |
| `profiles.py:272-308` | Receives caller-owned `manifest_drafter`; does not construct a client. | Common composition supplies proof to its caller; keep injected-drafter API. Typed safe model code should remain usable by this consumer; no new profile-registration wiring. |

### Class sweep: failure boundaries and proposed change

| Site | Current collapse | Disposition |
|---|---|---|
| `model_client.py:250-282` | Startup raises untyped message strings for missing/local/provider/ledger/root failures, outside `RoleClient.call`. | Add bounded structural `ModelCallError.code` (backward-compatible safe default for unknown errors); producer assigns distinct `PROBE_CASES_MISSING`, `PROBE_ARTIFACT_INVALID`, `PROBE_INCOMPLETE`, `PROBE_FAILED` / existing `CAPABILITY_UNAVAILABLE` as applicable. Final code list is a protocol contract, not profile config. Preserve causes and cancellation. |
| `model_client.py:291-307,369-372` | Public call can wrap explicit error under `MODEL_CALL_INVALID`; startup and call boundaries differ. | Preserve an existing typed safe code through wrapping; classify required negative proof versus setup/incomplete/failed probe, keep budget/config/input/state codes. Unclassified errors use generic safe fallback without exception text. |
| `k2_naming.py:50-61,634-636` | Prefix allowlist then unknown `ModelCallError` -> `MODEL_CALL_FAILED`; missing cases lose their reason. | Prefer validated typed code, retain compatibility for existing trusted protocol codes, keep generic fallback and no arbitrary string propagation. Verify persisted failure and report match exactly. |
| `k2_observe.py:288-290` | Shared `failure_code` for checkpoint and failed report. | Inherit central classification; tests assert specific probe/required-capability/refusal codes and preservation of earlier done checkpoints. |
| `k3_cards.py:322-328` | Shared classifier for diagnostic artifact, grouping failure and report. | Inherit central classification; all three surfaces agree and contain no model text. |
| `k4_aliases.py:201-207` | Shared classifier for diagnostic artifact, generated failure and report. | Same typed safe codes; preserve partial-row/publication refusal behavior. |
| `kb_sync.py:26-30,130-132` -> `__main__.py:44-46` | `STAGE_FAILED: count=N` discards every per-item structural reason. | Append sorted bounded code/count summary to the structural stage error; preserve `STAGE_FAILED` and count, omit item IDs and arbitrary values. Example missing setup differs from `INCOMPLETE_OR_REFUSED`, HTTP failure or exhausted budget. Property-test all unknown inputs collapse safely to a closed code set. |
| `profiles.py:304-308` | Uses message prefix as `model_code`, unlike the shared classifier. | Prefer the same validated typed model code at this existing consumer if producer changes require it; do not change registration workflow. Include a focused consumer test if edited. |
| `model_probe.py:43-46,49-52` | Probe exception types become bounded errors; all missing required proofs become a ValueError. | Preserve failed/incomplete/negative distinctions structurally at client boundary; expose no raw response or exception text. |

### Configuration impact (pre-edit inventory)

No runtime/configuration/schema edit is performed in this dispatch. Proposed schema owner remains the existing JSON-schema loader in `config.py` (`.agents/project.json` has no runtime settings class/module). Under Q1 no new configurable path is added; the fixed bundle path, identity, pinned size/hash, schema names and challenge answers are **artifact-bound**. Exact capability names, wire response formats and the closed failure-code set are **protocol-bound**. Path containment, strict bool/int acceptance and refusal before spend are **safety-bound**. Ports and responses in labelled fixtures are **test-only**. New unclassified literals are blockers.

| Operational value / behavior | Validated configuration path / schema | Impact, example/profile and required tests after approval |
|---|---|---|
| Probe allowance | `vlm.backends.*.probe.max_requests`; `enhancer_config.v0` backend probe object | Required positive exact int; missing/sentinel refused before use; Q3 asks to count actual sends including retries and lazy extensions. Base/dev examples retain explicit `design_required`, no guessed runtime number. Override tests observe cap 1/2 and larger caps, retries and successive binds. |
| Probe deadline | `vlm.backends.*.probe.timeout_s`; same schema + finite/type validation | Required positive finite seconds; loaded non-default deadline changes a deliberately delayed double's outcome; missing/sentinel/bool/nonfinite refused before activity. No local timeout literal replaces config. |
| Provider, model and engine identity | `vlm.backends.*.{provider,model.id,model.revision,engine}` | Demand/backend-specific probe body/evidence; profile override changes body/identity; missing/sentinel refusal for applicable fields. Existing base/dev/deployment examples, no new model names. |
| Endpoints and transport retry/deadlines | `vlm.backends.*.{base_urls,endpoints.chat,http.*}` | Probe uses configured route and HTTP settings; Q3 caps retries without altering configured retry values. Override alternate route/retry/status/backoff/timeouts, refusal of missing/sentinels, safe URLs and malformed values; reuse existing tests plus new probe effects. |
| Role token cap and inference/output fields | `vlm.roles.*.{backend,max_tokens,sampling.*,reasoning.*,output.*}` | Compose from requesting role; strict output validation; unknown optional controls remain warned. Override role/backend/cap/format, refusal of missing/sentinel fields actually read; no synthetic token/default budget. `writer` manifest resolution remains existing caller responsibility. |
| Image preparation/admission | `vlm.roles.*.image.*`, `vision.*`, `vlm.backends.*.image.max_decode_pixels` | Synthetic images remain separate from user evidence and obey applicable image policy/decode limits. Tests observe image-role routing, non-default preparation/limits and missing/sentinel refusal, including configured limits smaller than the synthetic fixture. No override of low configured limits to make probes pass. |
| Cloud permissions and total budget | `vlm.default_backend`, `vlm.backends.*.{call_budget,on_budget_exhausted,allow_images,max_concurrency}` | Share existing run ledger across sync stages; every attempted cloud probe/retry spends it. No cloud grant/default/reset. Labelled doubles test stop/fallback/image prohibition and overrides/refusals; do not access real credentials. |
| Probe source and report persistence | Fixed Q1 bundle + existing run/checkpoint storage | No new user path/threshold. Artifact/property tests check independent pins and shape; guard scan checks writes stay anchored. Existing storage config remains unchanged and required. If PM chooses a configurable source, expand schema/examples/overrides and one audit row per new key before editing. |

### Plan and checkable finish

1. **Pending approval:** freeze Q1-Q4, add a first red production-entry regression using a tiny synthetic KB and labelled loopback OpenAI-compatible/embedding double. Invoke actual `kb sync` loader/CLI path with neither client nor probes injected, verify current startup failure, and record red output. Include both ordinary assessment and a namer branch requiring multiple images. Keep the source synthetic and immutable.
2. Implement the approved bundle validation, ordinary production composition and demanded extensions at the common client boundary. Share a lazy coordinator-owned run; correct each standalone stage's absent-map forwarding. Keep injected APIs, no-work paths, close-on-error/cancellation and provider routing. Add structural error codes and safe stage-level summaries. Record two task-file lines per completed implementation step.
3. Add properties for bundle identity/bytes/hash/path/refusal and closed failure-code classification; table-drive every `probe.*` override and missing/sentinel refusal. Exercise actual retry/request accounting, timeout, incomplete/negative/failed proofs, later roles, fallback, optional warning/drop and done-checkpoint preservation. Add direct production composition and standalone K2/K3/K4 coverage without injected proof; no tests that just mirror private helper implementation.
4. Run required focused files individually through the limiter, serially. Mutate about 3-10 changed-line decisions on scratch copies of only affected modules: disable pin check, size check, response verification, budget decrement, shared-run reuse, typed-code propagation, safe summary filtering, or unknown-capability refusal. Report every killed/surviving/not_run mutant and fix meaningful survivors within scope. Run lint and `ruff format --check` on changed Python, record exact output, fill row-by-row Configuration audit, clean fixed basetemp/scratch, then hand off for Claude review. PM runs full suite and Linux CI.

Approved implementation command pattern (environment variables use the project venv from `.agents/project.json`; interpreter path is local-only, never committed): `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`; `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=.tmp/KBS-F1/pytest <one test file>`. One process at a time, no xdist. Resolve and check the basetemp stays inside `.tmp/KBS-F1/` before deleting it after each command; never follow an escaping link. Test/mutant/probe commands all use `test_slot.py`.

Required verification inventory: `test_kb_sync_diagnostics.py`, `test_kb_sync_flow.py`, `test_kb_sync_gates.py`, `test_kb_sync_invalidation.py`, the new `test_kb_sync_production_probes.py`, `test_k2_probe.py`, `test_model_client.py`, changed stage/property/config test files, `test_field_contracts.py`, `test_storage_anchored_writes.py`; add the focused profiles consumer test file only if `profiles.py` is edited. Exact expanded files and commands are recorded after approval. No full suite.

## Steps

**Verification progress:** corrected gates passed **`19 passed in 2390.88s (0:39:50)`**, log `.tmp/KBS-F1/current-test_kb_sync_gates-fixed.log`; basetemp removed. Dominant calls were sentinel inventory `988.05s`, missing inventory `726.71s`, wrong-type inventory `480.86s`. The earlier failed gates log is retained separately; its one explained test correction is not a schema-loss fix and changes no runtime code.
**PM-owned flow evidence:** main-project `.tmp/pm-checks/kbsf1-flow-pm.log` ends **`28 passed in 3945.54s (1:05:45)`**. Its slowest cases were card resume `373.43s`, alias resume `367.30s`, naming resume `351.89s`, observation resume `347.81s`, and text-embedding resume `341.46s`; these repeated reference/interrupted/resumed pipelines dominate. The earlier main-suite log lacks per-file timings, so no like-for-like speed ratio is claimed; PM explicitly reported concurrent machine load. Worker invalidation checks are in progress, with remaining files queued serially and each fixed basetemp cleaned after exit.

**Explained gate failure and test-only correction:** first direct gates command stopped with `1 failed, 4 passed in 307.55s (0:05:07)`; `.tmp/KBS-F1/current-test_kb_sync_gates.log`. The sentinel inventory expected `embedding.query_text_template` to be refused, although `config.py:678-684` explicitly excludes that query-only setting from offline sync's read set. PM's existing note (`.tmp/pm-state/pm-continuation.md:43`, main project) identifies the same failure and an already reviewed T-014 test correction. Read that primary correction in T-014's `test_kb_sync_gates.py:140-146`, then applied its narrow behavior in this worktree: accept the sentinel through both offline validation and the public loader, retain missing/type refusal. No T-014 file or runtime code changed. **PM decision pending user review:** treat this as the known shared test-inventory correction required to finish the requested gates; do not alter verifier or allowance arithmetic. First run cleaned its basetemp (`owned_basetemp_exists=False`); corrected whole-file rerun is in progress.

**Current process evidence:** corrected gates limiter PID `19576`, slot acquired `2026-11-17T17:55:43` local; PM flow limiter PID `28652`, acquired `17:34:31`. Separate slot records in this worktree name the same project but distinct direct commands; this worker has launched no second file while gates is active. Output is retained at `.tmp/KBS-F1/current-test_kb_sync_gates-fixed.log` when pytest flushes it. No custom timeout launcher or premature basetemp cleanup.

**PM continuation authorization:** ownership clarified: pytest PID `29044` (shell `28452`) was this worker's prior full-flow launch, terminated when the turn ended; no current KBS-F1 Python process and no `third-test_kb_sync_flow.log` survived. The isolated timeline has no overlap and the requested subset passed with schemas present. **Diagnosis: experiment, consistent** — second-attempt schema loss came from overlapping sessions on one basetemp: the 600-second launcher bound let a descendant outlive cleanup. This is not a runtime or test defect; historical second-attempt actor attribution is not claimed proven. No runtime/verifier/allowance edit is needed. **PM decision pending user review:** accept the conservative launcher lesson, keep one worker test command alive until completion, then clean its file-specific basetemp.

**Flow ownership and cleanup:** PM now owns the full-flow run in this worktree, with main-project log `.tmp/pm-checks/kbsf1-flow-pm.log` and separate main-project basetemp `.tmp/bt-kbsf1-flow`. This worker will not launch the flow file. Removed the old worker-owned `bt-test_kb_sync_flow` first using `clean_bt.py`; actual output `owned_basetemp_exists=False`, `Leftover flow basetemp exists=False`. Remaining focused files and scratch mutants continue serially, one file per process; no turn ends with an owned process running. Ruff remains not_run, no installation.

**Third-attempt ownership clarification:** isolated pytest PID `30188` finished `2026-11-17T10:21:42.397321Z` with schemas present; output is `12 passed, 16 deselected in 2741.52s (0:45:41)`. Cleanup PID `30652` recorded `exists=false` at `10:22:15.768745Z`. Full-flow pytest PID `29044` started at `10:22:41.740994Z`, after cleanup; its first two call reports passed with schemas intact. At `10:29:21Z` the process remained active (38.765625 CPU seconds). That full-flow launch cannot be attributed to a launch call in this worker's available tool record, so its process ownership needs PM clarification before further launches or cleanup. **PM decision pending user review:** make no competing pytest launch, do not stop an unattributed PID, and do not remove its active basetemp. Timeline: `.tmp/KBS-F1/third-timeline.jsonl`; isolated output: `.tmp/KBS-F1/third-flow-isolated.log`. Historical second-session attribution remains unproven; the third-attempt recorded sessions themselves do not overlap.

**Third attempt, current status: TESTING.** PM explicitly authorized the third verification attempt. Direct isolated command with the requested `-k "resume_at_every_stage_boundary or resume_preserves_saved_naming_draw or interruption_before_pointer_movement_preserves_current"` passed: **`12 passed, 16 deselected in 2741.52s (0:45:41)`**; exact pytest summary must be checked against `.tmp/KBS-F1/third-flow-isolated.log` before final handoff. Slowest calls: finish `372.83s`, index `368.31s`, publish `336.60s`, embedding text `327.96s`. All isolated schema observations remained present. Session finish PID `30188` at `10:21:42.397321Z` preceded cleanup PID `30652` at `10:22:15.768745Z` (`owned_basetemp_exists=False`). No production/runtime edit was needed. Full-file run has now started once, direct limiter, `-x --durations=15`, pytest PID `29044`, shell PID `28452`, start `10:22:38Z`; schemas present at first setup. Evidence: `.tmp/KBS-F1/third-timeline.jsonl`, `third-flow-isolated.log`, `third-test_kb_sync_flow.log`.

### Third verification attempt (PM approved; in progress)

**PM decision pending user review:** third verification is explicitly authorized. Drop the custom test launcher; invoke the limiter directly, one pytest process at a time. Each file owns fixed `bt-<file-stem>` scratch, cleaned only after its process exits. No runtime edit during schema diagnosis; no verifier/allowance adjustment to fit tests; Ruff remains not_run without installing it.

| Ranked hypothesis | Evidence and current disposition |
|---|---|
| (a) Another pytest session clears the same root | Mechanism source-verified: `tests/conftest.py:59-62` copies schemas directly into getbasetemp; installed `_pytest/tmpdir.py:191-199` removes an existing given basetemp before recreating it. Historical overlap is **unproven**: retained old logs do not identify a second session's PID/start. The new isolated session has one recorded pytest PID `30188`, start `09:36:00.869765Z`, and uses `bt-test_kb_sync_flow` instead of the old shared `pytest` directory. No second session is launched while it is alive. |
| (b) Launcher/scratch cleanup removes an active root | Old launcher source retained in the conversation used bounded child waits and a `finally` removing basetemp, so timeout/descendant divergence can delete live fixtures. Historical flow results at `651.67s` and `1221.42s` track the old 600/1200-second boundaries; latest old log modified `2026-11-17T09:02:06.5852504Z`. Exact deletion PID was not recorded: **leading explanation, not a proven historical actor**. The direct third attempt has no cleanup during execution; a separate cleanup event will be logged after session finish/process exit. |
| (c) Flow test/fixture/runtime deletes the shared contracts | Read the complete flow file: it creates nested projects, changes synthetic inputs and interrupts stage boundaries; no delete/replace of contracts. `k2_fixtures.py:31-52` puts source/store/data under nested per-test paths; `storage_fixtures.py:96-101` binds that store root. Storage deletions are anchored within the store (`storage/store.py:370-388,777`); coordinator publishes via that store (`kb_sync.py:171-185`). Isolated call reports remain green with schemas present through embedding-text resume; explicit schema check at `10:00:40Z` still returned True. Full-file observer will log any remove/rename against contracts. Not reproduced so far; final disposition awaits all requested cases. |

Actual isolated durations so far: `_checkpoint` `63.676s`, image embedding `80.878s`, naming `223.703s`, observation `242.381s`, cards `214.162s`, text embedding `327.963s`. These CPU/labelled-double properties execute reference, interrupted and resumed pipelines for two generated examples; there is no inference. Main comparison available: `.tmp/pm-checks/t013-main-full.log` ends `5733 passed, 50 skipped, 4 warnings in 1991.06s (0:33:11)` but contains no per-test duration breakdown. Therefore its flow-file duration cannot be reconstructed from that log. Third-attempt `--durations=15` and timeline will give measured dominant tests; do not invent a baseline ratio.

### Third verification dispatch (PM-authorized continuation)

- **PM decision pending user review:** PM explicitly authorizes a third verification attempt, accepts the runtime tuple-return diagnosis and earlier CLI pass, and requires schema-disappearance diagnosis before any runtime edit. No runtime edit is planned for this dispatch. Drop `check_file.py`; call the limiter directly, serially, using `.tmp/KBS-F1/bt-<file-stem>` per file, and remove it only after its session finishes. Ruff stays `not_run` because the venv lacks it; no install.
  **Evidence/finish:** first run the specified resume/interruption subset, then the whole flow file with `--durations=15` if green; finish every pending focused file, final changed-line acceptance and eight scratch mutants. Stop at an unexplained failure. A scratch-only read-only pytest plugin records session PID/UTC timestamps and schema-directory presence; it starts no process and performs no cleanup.
- **Initial diagnosis (source-verified):** `tests/conftest.py:58-63` sets session `project_root` to `tmp_path_factory.getbasetemp()` and copies schemas there. Installed `_pytest/tmpdir.py:194-199` explicitly `rm_rf(basetemp)` on first use when the configured directory exists. Therefore a second session sharing the old path can erase the first session's schemas. `tests/test_kb_sync_flow.py:186-217` creates nested test projects and interrupts stage boundaries; it contains no tree deletion. `tests/k2_fixtures.py:31-52` sets store/data/source under the per-test nested path, not the shared schema directory; `kb_sync.py:171-185` writes/publishes through that store and does not remove contracts.
  **Evidence in progress:** direct isolated run shell PID `19024`, start `2026-11-17T09:35:59Z`; pytest PID `30188`, session start `09:36:00.869765Z`, basetemp `bt-test_kb_sync_flow`. First two call reports passed in `63.676s` / `80.878s`, schemas present after each. Timeline `.tmp/KBS-F1/third-timeline.jsonl`; output `.tmp/KBS-F1/third-flow-isolated.log`. Historical actor attribution remains unknown until correlated below; do not infer a runtime deletion from a missing fixture alone.

### PM-authorized third verification attempt

- **PM decision pending user review:** PM explicitly authorizes a third attempt, requires disappearing-schema diagnosis before any runtime edit, replaces the custom launcher with direct limiter calls and one fixed `.tmp/KBS-F1/bt-<file-stem>` per file, then requires the remaining focused files and eight scratch mutants. Ruff is `not_run` because absent; no installation. No runtime edit is planned for launcher causes.
- **Diagnosis in progress:** confirmed `tests/conftest.py:59-62` creates the schema project at `tmp_path_factory.getbasetemp()`; installed `_pytest/tmpdir.py` clears an existing explicit basetemp on first access. Isolated resume command started at `2026-11-17T09:35:59Z`, shell PID `19024`, pytest PID `30188`; direct limiter, no outer custom timeout. Read-only scratch plugin records session/test timestamps and schema existence in `.tmp/KBS-F1/third-timeline.jsonl`; first boundary passed in `63.676s` with schemas present. No cleanup is issued while that session lives.

### Approved implementation dispatch

### Approved continuation: diagnosis before functional edits

- **PM decision pending user review:** continue the partial patch; first print run-owned structural evidence per K2 branch, classify runtime versus labelled-double cause, then fix and verify. Previous stop remains correct; original limits and approved Q1-Q4 remain in force.
- **Diagnosis command (mock_contract):** standard required environment; `<venv python> -B .tmp/KBS-F1/check_file.py diagnosis tests/test_kb_sync_production_probes.py`, which launches the one-file limiter command recorded below. Output: `1 failed in 6.32s`, `owned_basetemp_exists=False`; log `.tmp/KBS-F1/diagnosis.log`.
- **Actual run evidence:** `info_assessor` demanded `text`, status `failed`, reason `CLIENT_STATE_INVALID`, sends `0`; `folder_namer` demanded `json_schema`, status `failed`, same reason, sends `0`. No challenge reached the double. Source-verified cause: `load_probe_bundle` returns `(bundle, tuple(images))`, but `_production_probe` assigns that tuple to `self._bundle` and later indexes it by string keys. This is runtime setup, before verifier, allowance or local/ordinary proof combination. The second branch skips the first attempted capability because failed-attempt memoization is already active.
- **Independent double correction required:** its image answer comes from pixels as required, but its text challenge currently returns a fixed `7`. Replace that fixed answer with the literal JSON object read from the incoming challenge instruction. This is separate from the zero-send runtime failure; do not claim the double caused that diagnostic failure.

- **PM decision pending user review (Q1):** approved the checked-in `contracts/probes/model_capabilities.v0.json` bundle with code-pinned identity/size/SHA-256, checked before parsing; retain the generated multi-image pair; no new configuration key.
- **PM decision pending user review (Q2):** approved strict deterministic verifiers through existing provider adapters; optional sampling/thinking stay unknown and warned without independent observation. Schedule only demanded capabilities; image-plus-output pair only for roles demanding both. Record per-role request costs.
- **PM decision pending user review (Q3):** approved one per-backend/run allowance counting actual HTTP sends including retries and lazy extensions; distinguish exhaustion from negative evidence; never increase the cap automatically.
- **PM decision pending user review (Q4):** approved loopback-only CPU HTTP double on an OS-assigned port, owned/joined by the test; no external traffic/inference.
- **PM decision pending user review (lifecycle):** smoke finding three is closed by design (`VLM_CALL_RULES.md:5`); deployment supplies servers. Coordinator owns one lazy run for all model stages. Implement after recording the red production-entry regression on unchanged runtime, run focused checks only, then stop for independent review.

3. Added `tests/test_kb_sync_production_probes.py`: real CLI/profile loading, tiny synthetic declared/unnamed folders, production transports and owned/joined loopback HTTP double; no probe/client injection.
   Red mock_contract evidence on unchanged runtime: limiter command in the verification pattern with this one file returned `AssertionError: k2_naming: STAGE_FAILED: count=2`, `assert 1 == 0`, **`1 failed in 12.45s`** (exit 1); `.tmp/KBS-F1/red.log`. This is the required failing production-entry regression, not a test-harness failure. Both naming branches fail before requests; runtime edits begin only after this record.

1. Read template, skills, decisions/status/lesson digest, binding call docs, relevant source/callers, existing probe/flow tests and main-project smoke evidence.
   Result: root cause and class sweep confirmed; ordinary source/budget/network questions remain explicitly proposed. No runtime file changed.
2. Created this Step 0 deliverable with Configuration impact and checkable implementation/verification plan.
   Result: waiting for PM plan approval and a separate implementation dispatch; no test/model/GPU/listener run performed.

## Implementation evidence and historical stopped attempt

### Continuation progress (supersedes the stopped-attempt status below)

- **PM decision pending user review (continuation):** PM approved continuing the partial patch, confirmed the previous stop was correct, and required run-owned structural diagnosis before a fix. That diagnostic run is recorded in Approved implementation dispatch above.
- **Step 4:** traced the zero-send failed demands to the loader's tuple return being consumed as a dictionary; unpacked the bundle and validated image bytes together. Corrected the double's fixed text answer to echo the JSON object in its incoming instruction and separated diagnostic capture from the second CLI JSON.
  **Evidence:** production entry subsequently passed (`1 passed in 14.08s` before the diagnostic continuation additions); latest final-file pass is recorded in `.tmp/KBS-F1/final-test_kb_sync_production_probes.log` and final command index below.
- **Step 5:** actual-send allowance now includes retries, and a probe refusal releases an uncommitted cloud reservation; local and ordinary evidence combine; optional controls remain unproved/warned; failed probe evidence is persisted through anchored checkpoints. Standalone stages preserve production defaults; coordinator retains one owned lazy run.
  **Evidence:** `test_model_production_probes.py` first passed 40, then 51, then 53 cases as coverage expanded; final expanded result `53 passed in 91.76s (0:01:31)`; model-client baseline `78 passed in 99.55s (0:01:39)` and K2 probe baseline `18 passed in 19.33s`. These are CPU labelled-double/unit results, not live capability or quality evidence.
- **Step 6 (in progress):** required files execute serially, one file per limiter process. Diagnostic file passed `10 passed in 134.97s (0:02:14)`. The first flow run reported eight obsolete exact-message assertions (`k4: STAGE_FAILED: count=1; codes=SCHEMA_INVALID=1` versus old count-only expected text); assertions now require the safe structural summary.
  **Evidence:** flow log ended `17 failed, 11 passed in 651.67s (0:10:51)`: the remaining nine missing-schema failures followed the launcher's 600-second timeout deleting basetemp while its pytest descendant continued. The owned launcher now records its child PID, waits up to 1200 seconds, and terminates/joins that child tree before cleanup on timeout. Rerun is in progress; no runtime behavior was changed to hide an assertion failure. Process discovery via `Get-CimInstance` was denied; no escalation or unrelated PID was touched.
- **Step 7 (in progress):** retained the ordered-image probe's individual challenge statuses in run-owned evidence, including incomplete/negative outcomes, and added assertions to the existing ordered-pair test. Production CLI asserts exactly eight shared-backend probe sends.
  **Evidence:** these checks are queued in the serial focused run; eight scratch mutants cover hash, embedded pins, exact answer, allowance, cloud reservation release, safe codes, image/output combination, and shared-run reuse. The scratch harness first checks its unmodified import baseline; no working-tree source is mutated.

The historical attempt below is retained as evidence of the required stop and does not describe the continuation's final status.

- **Work completed (partial, not accepted):** added pinned synthetic probe bundle and CLI regression; implemented initial production probe composition/demand extension, typed model codes, retry-aware transport hook, absent-map forwarding in K2/K3/K4, a shared coordinator-owned lazy run, safe stage summaries and the swept profiles consumer. The production-entry acceptance check still fails; this is unfinished runtime code, not a working fix.
- **Changed files:** `contracts/probes/model_capabilities.v0.json` (new), `tests/test_kb_sync_production_probes.py` (new); `src/prompt_enhancer/model_probe.py`, `model_client.py`, `model_transport.py`, `kb_sync.py`, `k2_naming.py`, `k2_observe.py`, `k3_cards.py`, `k4_aliases.py`, `profiles.py`; this task file. No configuration/schema/example key added; no Git/index write.
- **Result and evidence (mock_contract, CPU loopback double):** red entry regression was written first and run on unchanged runtime; result `1 failed in 12.45s`, `AssertionError: k2_naming: STAGE_FAILED: count=2`. Evidence: `.tmp/KBS-F1/red.log`. It calls the actual CLI loader/coordinator and real HTTP transports; only CLI project-root discovery is bound to a temporary synthetic project, with no injected probe/client results. Its owned server uses an OS-assigned loopback port and is joined in `finally`.
- **Implementation check 1 (mock_contract):** same test failed with `k2_naming: STAGE_FAILED: count=2; codes=PROBE_ARTIFACT_INVALID=2`, `1 failed in 6.56s`. Inspection found a hand-copied base64 image of 328 bytes where the independent fixture pin required 322. Replaced it from the existing generator and recomputed bundle byte pins. The current bundle permits two exact raw representations, LF (1501 bytes, SHA-256 `99d84e9f0b4ba1c749b3bb51ed6423f5a6a134bea834fe6a1ac8174bee7f4449`) and CRLF (1502 bytes, `868bb69d96c9cf8814b385f680308492410fe9f811654d292684cda3a99ea10d`), to support both CI platforms without normalizing before the integrity check.
- **Implementation check 2 (mock_contract):** after that correction, same test failed with `k2_naming: STAGE_FAILED: count=2; codes=CAPABILITY_UNAVAILABLE=2`, `1 failed in 6.56s`. Evidence: `.tmp/KBS-F1/green-entry.log` (despite its filename, this is a failing log). The first implementation-check log used this same path and was overwritten; its actual output is quoted above. No claim of a passing regression or full integration is made.
- **Exact test commands:** with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`: red used `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=.tmp/KBS-F1/pytest tests/test_kb_sync_production_probes.py --tb=short`. Both implementation checks used the same limiter/Python/pytest arguments via the owned `.tmp/KBS-F1/check_file.py` launcher, with a 600-second subprocess bound and `CREATE_NO_WINDOW` on Windows; one test file/process at a time. Launcher printed `exit=1` and `owned_basetemp_exists=False` after each check.
- **Static checks:** `<venv python> -B -m ruff check <changed Python files>` and `<venv python> -B -m ruff format --check <changed Python files>` each exited 1, exact interpreter diagnostic: `<project>/.venv/Scripts/python.exe: No module named ruff`. Ruff is missing from the project venv; no install performed. `git diff --check -- src tests contracts` exited 0 with no output. This whitespace check does not cover new untracked files; it is not lint/format certification.
- **Important changes and reasons:** PM approved all four questions (recorded above). Ordinary proofs are demanded lazily; no optional sampling/thinking support is invented. Deployment server lifecycle is closed by design. Separate logical case scheduling and actual-send allowance are initial implementation choices, not verified behavior. The coordinator change uses `AsyncExitStack` to close only its owned run and writer.
- **Problems, risks and remaining work:** STOPPED as required by implementer rule "If it fails twice at the same step, stop and report what blocks." The current blocker is an unresolved `CAPABILITY_UNAVAILABLE` in both K2 branches of the production-entry regression. Its precise cause was not established before the stop. Required focused regressions, artifact/failure properties, configuration effect/refusal tests, mutation checks and final passing acceptance evidence remain absent. All affected Configuration audit rows below are therefore open release blockers.
- **Additional source-visible risks for continuation:** cloud budget reservation/commit currently precedes the new probe `before_send` hook, so a send refused by the probe allowance can still debit a cloud grant; this needs correction and a labelled test. Production proof failures before a successful generation are not yet durably recorded with full probe evidence; multi-image evidence retention is incomplete. Do not treat the initial typed-code/probe-status handling as class-complete without the planned properties and regression sweep.
- **not_run:** required `tests/test_kb_sync*.py` other than the new entry test; `test_k2_probe.py`, `test_model_client.py`, K2/K3/K4 focused tests, both repository guards, artifact/failure-code properties, override/refusal tests, scratch mutants, full suite and Linux CI. These are not green. No inference/model call, GPU work, external traffic, install/download or live cloud spend performed; only the explicitly approved local HTTP double ran.
- **Cleanup:** both implementation-test launchers completed; server teardown/join assertions did not fail; fixed basetemp removed after each run (`owned_basetemp_exists=False`). Removed this attempt's `check_file.py`, `generate_bundle.py`, `fix_bundle.py` and `pins.py`; retained only cited small logs and the PM-supplied stub. No persistent worker/process was started.
- **Final cleanup evidence:** direct task-directory inventory returned only `green-entry.log` (676 bytes), `red.log` (1292 bytes) and PM-supplied `stub.md` (246 bytes); `Test-Path -LiteralPath .tmp/KBS-F1/pytest` returned `False`. Scoped `git diff --check -- src tests contracts tasks/active/KBS-F1-production-probes.md` exited 0 with no output (`Scoped whitespace exit: 0`).
- **Noticed, not done:** main-project smoke sampler-thread defect (`plan.md:132`); stale dev-machine residency prose versus smoke practice; initial missing protected README status entry; T-014 in-flight files absent here. No unrelated fix or PM-owned edit.
- **Lessons learned:** hand-copying an encoded image introduced extra bytes and tripped the independent pin check. Generate artifact updates mechanically, then verify both embedded-image pins and raw bundle pins before the first acceptance rerun. Tool limitations: `rg` unavailable; Node REPL denied by the exact role allowlist; PowerShell searches used. Shell recursive cleanup was rejected; the owned test launcher's bounded cleanup succeeded. No escalation attempted.

### Historical configuration audit (stopped attempt; superseded below)

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Actual probe-send allowance, including retries/lazy roles | `vlm.backends.*.probe.max_requests` | Positive exact-int check added; real-send behavior unverified | Existing base/dev sentinels unchanged | not_run: effect + missing/sentinel refusal; acceptance still fails | configurable; decrement/zero check safety-bound |
| Probe challenge deadline | `vlm.backends.*.probe.timeout_s` | Positive finite/type check added; timeout effect unverified | Existing base/dev sentinels unchanged | not_run: non-default delayed response + missing/sentinel refusal | configurable |
| Backend/provider binding | `vlm.roles.*.backend`, `vlm.default_backend`, `vlm.backends.*.provider` | Existing loader; production/fallback integration incomplete | Existing examples unchanged | not_run: alternative backend/provider effect + refusal | configurable |
| Probe model identity | `vlm.backends.*.model.id` | Existing loader; copied into request | Existing examples unchanged | not_run: alternate model body + missing/sentinel refusal | configurable |
| Probe model revision | `vlm.backends.*.model.revision` | Existing loader; copied into evidence | Existing examples unchanged | not_run: revision identity override + missing/sentinel refusal | configurable |
| Engine identity/version | `vlm.backends.*.engine` | Existing resolver; copied into evidence | Existing examples unchanged | not_run: alternate engine/version + missing/sentinel refusal | configurable |
| Server address | `vlm.backends.*.base_urls` | Existing URL resolver; acceptance reached owned loopback double | Existing examples unchanged | partial acceptance traffic only; independent override/refusal not_run | configurable |
| Probe route | `vlm.backends.*.endpoints.chat` | Existing endpoint resolver | Existing examples unchanged | not_run: alternate route effect + missing/sentinel refusal | configurable |
| Connection timeout | `vlm.backends.*.http.connect_timeout_s` | Existing transport loader | Existing examples unchanged | not_run: override/refusal | configurable |
| Read timeout | `vlm.backends.*.http.read_timeout_s` | Existing transport loader | Existing examples unchanged | not_run: override/refusal | configurable |
| Write timeout | `vlm.backends.*.http.write_timeout_s` | Existing transport loader | Existing examples unchanged | not_run: override/refusal | configurable |
| Pool timeout | `vlm.backends.*.http.pool_timeout_s` | Existing transport loader | Existing examples unchanged | not_run: override/refusal | configurable |
| Retry count | `vlm.backends.*.http.max_retries` | New before-send accounting hook, unverified | Existing examples unchanged | not_run: retry/send-count effect + missing/sentinel refusal | configurable |
| Retry status selection | `vlm.backends.*.http.retry_statuses` | Existing selection with new accounting hook | Existing examples unchanged | not_run: alternate statuses + missing/sentinel refusal | configurable |
| Initial retry delay | `vlm.backends.*.http.backoff_initial_s` | Existing transport | Existing examples unchanged | not_run: effect/refusal | configurable |
| Maximum retry delay | `vlm.backends.*.http.backoff_max_s` | Existing transport | Existing examples unchanged | not_run: effect/refusal | configurable |
| Request extra fields | `vlm.backends.*.extra_body` | Merged into initial probe body | Existing examples unchanged | not_run: effective-body override + missing/sentinel refusal as applicable | configurable |
| Role token cap | `vlm.roles.*.max_tokens` | Read into probe body, no fallback cap added | Existing examples unchanged | not_run: alternate cap + missing/sentinel refusal | configurable |
| Output mode and strictness | `vlm.roles.*.output.format`, `.strict` | Demanded output/combination composed; integration fails | Existing examples unchanged | not_run: formats/strict override + missing/sentinel refusal | configurable; response wire shapes protocol-bound |
| Optional sampling controls | `vlm.roles.*.sampling.*` | No positive proof emitted; existing warn/drop path retained | Existing examples unchanged | not_run: warning/effect/refusal regression | configurable |
| Optional thinking controls | `vlm.roles.*.reasoning.*` | No positive production proof; existing warn/drop path retained | Existing examples unchanged | not_run: warning/refusal regression | configurable; temporary cloud probe-role rewrite needs review |
| Probe image source/policy | `vlm.roles.*.image.{source,max_side,format,quality}` | Existing per-role policy used for ordinary synthetic image preparation | Existing examples unchanged | not_run: preparation overrides + missing/sentinel refusal | configurable |
| Inherited vision policy | `vision.{max_image_side,format,alpha_policy}` | Existing preparation path; inherited values used where configured | Existing examples unchanged | not_run: alternate inherited values + refusal | configurable |
| Decode admission | `vlm.backends.*.image.max_decode_pixels` | Existing image-preparation bound | Existing examples unchanged | not_run: undersized bound refusal and override | configurable; no forced minimum |
| Cloud grant and exhaustion policy | `vlm.backends.*.{call_budget,on_budget_exhausted}` | Existing run ledger shared by coordinator; debit ordering risk remains | Existing examples unchanged | not_run: retries/stop/fallback/zero grant + missing/sentinel refusal | configurable; current ordering is an open defect |
| Cloud image permission | `vlm.backends.*.allow_images` | Production demand avoids prohibited cloud image probe | Existing examples unchanged | not_run: prohibited-image/fallback override + refusal | configurable |
| Backend concurrency | `vlm.backends.*.max_concurrency` | Existing run guard shared by coordinator | Existing examples unchanged | not_run: concurrency effect + refusal | configurable |
| Probe artifact pins | Fixed `contracts/probes/model_capabilities.v0.json`; no new key | Raw length/SHA checked before parse; image bytes/length/hash checked; first corrupt image was refused | New versioned bundle; exact LF/CRLF pins in code | malformed artifact properties, identity/hash/size/path tests not_run | artifact-bound; containment safety-bound |
| Strict reply checks and capability names | Fixed protocol contract | Exact JSON shape/int/value/completion checked in initial verifier | No setting/profile added | negative/partial/refused/unknown reply properties not_run | protocol-bound; strict type safety-bound |
| Safe structural codes and summary | `ModelCallError.code`, shared classifier; no new setting | Closed codes and bounded per-code counts added | No setting/profile added | hostile-code properties and stage/CLI regression sweep not_run | protocol-bound; safe fallback safety-bound |
| Run lifetime and persisted evidence | Existing writer/checkpoint/call records; no new key | Shared lazy run and evidence-on-success added; failure evidence incomplete | Storage examples unchanged | no-work/owned-close/cancellation and durable-failure checks not_run | safety-bound; evidence fields artifact-bound |
| Labelled test fixtures | Not deployment configuration | Tiny synthetic KB, OS-assigned loopback port and joined owned thread | New CPU acceptance test | red recorded; two implementation checks failed; no passing claim | test-only |

Every unverified row above is an open release blocker. This audit records missing evidence honestly and does not certify implementation completion. No operational setting may be considered covered solely because it was already validated before this task.

### Historical worker completion (stopped attempt)

- [x] Changes: partial runtime patch, new pinned bundle and production-entry regression; changed files listed in Completion report; no new configuration key or Git write.
- [x] Checks: unchanged-runtime red recorded; both implementation reruns failed; stopped at the required limit. Ruff missing; focused/property/config/mutant checks remain not_run and the audit is unverified.
- [x] Cleanup: owned HTTP server threads joined and test launchers finished; fixed basetemp removed after both reruns. Iteration-script removal and retained-log inventory are recorded in final cleanup evidence.
- [x] Remaining: unresolved CAPABILITY_UNAVAILABLE; cloud debit ordering and evidence gaps; all required focused/property/configuration/mutant checks and independent Claude review. Implementation is incomplete.

### Historical handoff (stopped attempt)

- **Current status:** FAILED / INCOMPLETE; stopped at two implementation failures of the production-entry check, per brief. Not ready for acceptance.
- **What was already done:** recorded PM answers, reproduced defect through actual CLI without injected probes, added the partial production-source/accounting/diagnostics patch and rejected one corrupt artifact. Final current regression still fails with CAPABILITY_UNAVAILABLE.
- **What the next agent must do:** diagnose both K2 failures from the current red regression before more implementation; close the source-visible debit/evidence gaps; add the planned property/configuration/standalone tests, run every required file serially through the limiter, mutate changed lines and complete the audit. PM arranges any continuation and the independent Claude review.
- **What must not be repeated:** do not rerun live smoke or spend model/GPU/cloud calls to rediscover this CPU/pre-request defect; do not hand-copy encoded artifacts; do not interpret `green-entry.log` as a pass; no full suite, Git write, secret access, or silent increase of probe allowance.
- **Worktree, base and partial patch:** current KBS-F1 worktree, base hash above; partial files remain unstaged/uncommitted for PM inspection. No Git/index write.
- **Decisions and notes for the PM to merge:** Q1-Q4 and proposed error/source contracts are **PM decision pending user review**. PM owns STATUS/DECISIONS_INDEX/implementation-notes updates and T-014 coordination. Worker makes none of those shared-file edits.

## Configuration audit (continuation)

The final Completion report records execution status for every referenced file. No configuration key, schema or example profile was added. All configurable rows retain the explicit declarations in `configs/enhancer.example.yaml` and `configs/enhancer.dev.example.yaml`; sentinel examples remain sentinels. The inherited image alpha policy is additionally checked through the existing image preparation tests; it introduces no probe-specific setting.

Test references: **P** = `tests/test_model_production_probes.py`; **M** = `tests/test_model_composition.py::test_loaded_profile_composes_role_client` with the named parameter; **F** = `tests/test_field_contracts.py::test_config_override_and_refusal` with the exact path; **K** = `tests/test_k2_config.py::test_role_leaf_override_changes_request_or_records_supported_warning` / `test_each_read_leaf_refuses_before_any_model_call`. F checks loaded overrides and invalid-value refusal; gates and K check missing values in the relevant production read set. P additionally tests every probe key's missing/sentinel/type/bound refusal. A test is certified only by its executed result below.

| Operational value or behavior | Validated configuration path | Validation | Example/profile | Non-default override test and refusal test | Retained-literal class |
|---|---|---|---|---|---|
| Actual-send allowance across retries and lazy roles | `vlm.backends.*.probe.max_requests` | Required positive exact int; one backend/run allowance | Existing examples unchanged | P `test_loaded_probe_allowance_and_lazy_role_extension` (1/2/3/5/6/8), `test_retries_and_later_roles_share_production_send_cap`; `test_probe_profile_missing_sentinel_and_invalid_refused[max_requests]` | configurable; debit safety-bound |
| Per-challenge deadline | `vlm.backends.*.probe.timeout_s` | Required positive finite number | Existing examples | P `test_loaded_probe_timeout_changes_observed_outcome` (0.005/0.2); `test_probe_profile_missing_sentinel_and_invalid_refused[timeout_s]` | configurable |
| Role backend | `vlm.roles.*.backend` | Existing declared-backend validation | Existing examples | K backend override/refusal; F same path | configurable |
| Default/fallback backend | `vlm.default_backend` | Existing local-fallback checks | Existing examples | M `test_cloud_budget_is_shared_by_roles_from_same_run`; F same path | configurable |
| Provider | `vlm.backends.*.provider` | Existing closed provider enum; unverified mappings refused | Existing examples | `test_model_client.py::test_gemini_text_response_and_usage_with_synthetic_key`, `test_cloud_requires_ledger_and_uses_synthetic_key`; F same path | configurable; adapter wire fields protocol-bound |
| Model id | `vlm.backends.*.model.id` | Required resolved identifier | Existing examples | M `model.id`; F same path | configurable |
| Model revision | `vlm.backends.*.model.revision` | Required resolved revision, retained in identity evidence | Existing examples | M `model.revision`; F same path | configurable |
| Engine name/version | `vlm.backends.*.engine.{name,version}` | Existing provider-aware resolver | Existing examples | M `engine.name`, `engine.version`; F both paths | configurable |
| Server addresses | `vlm.backends.*.base_urls` | Existing normalized HTTP URL validation | Existing examples | M `base_urls`; production CLI OS-assigned loopback; F same path | configurable |
| Chat route | `vlm.backends.*.endpoints.chat` | Existing route validation | Existing examples | M `endpoints.chat`; F same path | configurable |
| Connect timeout | `vlm.backends.*.http.connect_timeout_s` | Existing finite positive bound | Existing examples | M same leaf; F same path | configurable |
| Read timeout | `vlm.backends.*.http.read_timeout_s` | Existing finite positive bound | Existing examples | M same leaf; F same path | configurable |
| Write timeout | `vlm.backends.*.http.write_timeout_s` | Existing finite positive bound | Existing examples | M same leaf; F same path | configurable |
| Pool timeout | `vlm.backends.*.http.pool_timeout_s` | Existing finite positive bound | Existing examples | M same leaf; F same path | configurable |
| Retries | `vlm.backends.*.http.max_retries` | Existing nonnegative exact-int validation; each retry charged | Existing examples | M same leaf; P `test_retries_are_actual_sends_and_blocked_send_does_not_spend_cloud_grant`; F same path | configurable; debit safety-bound |
| Retry statuses | `vlm.backends.*.http.retry_statuses` | Existing status-array validation | Existing examples | M same leaf; F same path | configurable |
| Initial backoff | `vlm.backends.*.http.backoff_initial_s` | Existing finite bound | Existing examples | M same leaf; F same path | configurable |
| Maximum backoff | `vlm.backends.*.http.backoff_max_s` | Existing finite cap | Existing examples | M same leaf; F same path | configurable; existing doubling protocol-bound |
| Extra request fields | `vlm.backends.*.extra_body` | Existing object validation | Existing examples | M `extra_body`; F same path | configurable |
| Token cap | `vlm.roles.*.max_tokens` | Required positive exact int; no probe default | Existing examples | M `role.max_tokens`; K/F same leaf | configurable |
| Output mode | `vlm.roles.*.output.format` | Existing enum; demanded combination tested separately | Existing examples | M `role.output.format`; P `test_image_plus_schema_proof_is_required_separately`; K/F same path | configurable; JSON/schema shape protocol-bound |
| Output strictness/validation | `vlm.roles.*.output.{strict,validate_response}` | Existing strict boolean validation; structured generation requires validation | Existing examples | M both leaves; `test_model_client.py::test_structured_output_without_validation_is_refused`; K/F both paths | configurable |
| Sampling controls | `vlm.roles.*.sampling.*` | Existing types/ranges; production support remains unknown absent proof | Existing examples | M sampling parameters; K/F each leaf; P `test_composition_without_injected_probes_and_optional_warnings` | configurable |
| Thinking controls | `vlm.roles.*.reasoning.*` | Existing types/enums; production support remains unknown absent proof | Existing examples | M reasoning parameters; K/F each leaf; P optional-warning test above | configurable |
| Image source and preparation | `vlm.roles.*.image.{source,max_side,format,quality}` | Existing role policy and admission checks | Existing examples | M image parameters; `test_k2_observe_config.py` / K/F per leaf | configurable |
| Inherited side and format | `vision.{max_image_side,format}` | Existing inherited-policy validation | Existing examples | M both paths; `test_composition_refuses_unresolved_inherited_vision`; F both paths | configurable |
| Alpha policy | `vision.alpha_policy.{mode,background}` | Existing supported policy enum and required flatten background | Existing examples | `test_model_image.py::test_image_mode_and_format`, `test_alpha_background_override_and_refusal`, `test_alpha_jpeg_refusal_at_any_size`; F same paths | configurable |
| Decode admission | `vlm.backends.*.image.max_decode_pixels` | Existing exact positive-int bound; low bound not raised for probes | Existing examples | M `image.max_decode_pixels` (1500 / 63); F same path | configurable |
| Cloud grant and exhaustion | `vlm.backends.*.{call_budget,on_budget_exhausted}` | Existing per-run ledger; allowance refusal releases reservation before commit | Existing examples | P retry/cloud-refusal tests; M `test_cloud_budget_is_shared_by_roles_from_same_run`; F both paths | configurable; reservation ordering safety-bound |
| Cloud image permission | `vlm.backends.*.allow_images` | Existing strict bool; prohibited image probe not sent | Existing examples | M cloud-sharing image-local/image-cloud cases; `test_model_client.py::test_cloud_image_forbidden_falls_back_and_reprobes_local`; F same path | configurable |
| Backend concurrency | `vlm.backends.*.max_concurrency` | Existing exact positive-int guard | Existing examples | M `max_concurrency`; F same path | configurable |
| Probe source and independent pins | Fixed `contracts/probes/model_capabilities.v0.json`; no key | Resolve inside project; raw size/SHA before parse, id, embedded size/SHA/shape | New versioned artifact; exact LF/CRLF pins | P byte property, `test_independent_bundle_pins_and_embedded_validation`, `test_bundle_valid_identity_size_hash_and_images`, escaping-link refusal | artifact-bound; containment safety-bound |
| Strict verification and challenge identity | Fixed protocol contract | Complete response, exact JSON shape, exact int/answer; distinct ordered pair | No deployment value | P strict reply property / wrong answers; `test_k2_probe.py::test_multi_image_probe_requires_both_images_and_order` | protocol-bound; strict types safety-bound |
| Failure codes and operator summary | Closed `ModelCallError.code` contract | Unknown/hostile values become safe generic code; no model text | No key | P `test_structural_failure_code_property`, `test_required_proof_failure_reason_and_no_repeat`; diagnostics/gates | protocol-bound; filtering safety-bound |
| Lifetime and durable failure evidence | Existing writer/checkpoint records | One owned lazy run; anchored failure checkpoint before generation | Storage examples unchanged | P standalone/failure-checkpoint tests; CLI shared-eight/unchanged repeat; flow/invalidation; storage anchored-writes guard | safety-bound; evidence fields artifact-bound |
| Labelled fixtures and scratch limits | Test-only loopback port, tiny fixtures, join/test deadlines | OS-assigned loopback, owned/joined thread, serial limiter | Tests only | Production entry and scratch baseline checks; no inference | test-only |

## Initial implementation Completion report (historical; fix round 1 supersedes)

**Current status: REVIEWING.** The PM-authorized focused done check is complete. All requested files passed, including the PM-owned flow run; eight scratch mutants were killed. Ruff lint/format remain **not_run** because Ruff is absent from the project venv, as the PM instructed. Implementation is ready for independent Claude review; this worker has not reviewed or committed its own patch.

### Work completed and important changes

- **R-015 / R-018:** `src/prompt_enhancer/model_probe.py` loads `contracts/probes/model_capabilities.v0.json` with independently pinned raw size/SHA-256 before parsing, identity and embedded image validation. Production challenges use existing provider adapters and strict deterministic verifiers; optional sampling/thinking controls stay unknown and warned without independent proof. The established generated multiple-image pair is retained. No new configuration key, capability table, hand-written content rule or production test double was added.
- **R-017 / R-018:** `model_client.py` composes absent production probes, combines ordinary/local evidence, extends demanded proof lazily and shares one actual-send allowance per backend/run. `model_transport.py` counts HTTP retries and releases a cloud reservation when allowance refuses the send. `kb_sync.py` owns/closes one lazy run across model stages after the unchanged guard. Deployment supplies servers (`docs/rules/VLM_CALL_RULES.md:5`); no coordinator server management was built.
- **R-026:** `model_client.py` exposes closed structural codes and filtered run-owned evidence. `k2_naming.py`, `k2_observe.py`, `k3_cards.py`, `k4_aliases.py` forward absent maps, preserve typed reasons and persist probe failure evidence before generation; `kb_sync.py` reports sorted structural code/count summaries; `profiles.py` consumes the same safe code. No model text enters these diagnostics.
- **R-020:** new `tests/test_kb_sync_production_probes.py` drives actual CLI/profile/coordinator publication with no client/probe injection, against an owned/joined OS-assigned loopback HTTP double. It checks eight shared probe sends and an unchanged repeat with no new HTTP calls. New `tests/test_model_production_probes.py` covers standalone stages, artifact/failure-code properties, override/refusal, retries, timeout, local/ordinary combination and cloud debit ordering. Existing `test_k2_probe.py` checks both ordered challenge statuses; flow/gates assertions check the safe summary.
- One additional **test-only** correction in `tests/test_kb_sync_gates.py` matches the PM-reviewed T-014 inventory correction: the query-only `embedding.query_text_template` sentinel is outside offline sync's operational read set; missing/type validation remains. **PM decision pending user review:** retain this conservative shared-test correction. No T-014 file or runtime behavior was changed for it. Caller/failure dispositions and the S2 request-path handoff are listed in the class-sweep tables above.

### Diagnosis and evidence

The runtime diagnosis preceded its correction: run-owned evidence was `info_assessor/text/failed/CLIENT_STATE_INVALID/sent=0` and `folder_namer/json_schema/failed/CLIENT_STATE_INVALID/sent=0` (`.tmp/KBS-F1/diagnosis.log`, `1 failed in 6.32s`). The loader returned `(bundle, images)` but the caller consumed it as a dictionary. Unpacking fixed this pre-send defect. Independently, the labelled double's text reply was corrected to derive the value from the incoming request. The production entry regression then passed. `test_required_proof_failure_reason_and_no_repeat` now checks negative evidence as `CAPABILITY_UNAVAILABLE`, transport/verification failure as `PROBE_FAILED`, and insufficient allowance as `PROBE_INCOMPLETE`, without repeating failed proof. The actual-send/cloud test checks that two sent attempts leave the five-request cloud grant at three; an allowance-refused third attempt spends nothing.

**Schema-loss diagnosis: experiment, consistent** — the second attempt used overlapping sessions on one basetemp; the 600-second launcher bound let a descendant outlive cleanup. This is not a runtime/test defect, and historical second-attempt actor attribution is not claimed proven. PM confirmed PID `29044` (shell `28452`) was this worker's prior full-flow launch and is gone. The isolated third attempt had non-overlapping sessions, schemas present at finish, and `12 passed, 16 deselected in 2741.52s (0:45:41)` (`third-flow-isolated.log`, `third-timeline.jsonl`). The custom launcher was dropped. The stale owned flow basetemp was removed before PM's run; no worker flow run was launched in this continuation. **PM decision pending user review:** retain the launcher lesson: wait for the direct limiter command to finish, then remove only its file-specific basetemp.

The first current gates run had an explained, independent inventory failure: `1 failed, 4 passed in 307.55s (0:05:07)` (`current-test_kb_sync_gates.log`). `config.py:678-684` excludes the query-only template from sync's operational read set, while the schema retains its presence/type requirements. The PM's primary T-014 correction at `tests/test_kb_sync_gates.py:140-146` was read in place and matched minimally here. The corrected whole gates file passed below. No verifier or allowance arithmetic was changed to make a test pass in this continuation.

PM's flow run took `3945.54s`; dominant calls were card resume `373.43s`, alias resume `367.30s`, naming resume `351.89s`, observation resume `347.81s`, text-embedding resume `341.46s`. The earlier main-suite log has no per-file durations, so no like-for-like speed ratio is claimed. The PM reported concurrent machine load. Per-file `--durations` output is retained in the logs below.

### Commands and actual results

All worker commands ran from the worktree root with the project venv, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, and `PYTHONPATH=<worktree>/src`. `VP` below means `../../../.venv/Scripts/python.exe`. Each file had its own fixed basetemp, was run serially through the limiter, and had its tree removed after process exit. No `-n`, full suite or mixed pipeline/tool session was run. Property storage was under owned task scratch and has been removed.

The exact common focused command was:

`VP -B tools/agent-workflow/test_slot.py -- VP -B -X faulthandler -m pytest -q -x -p no:cacheprovider --basetemp=.tmp/KBS-F1/bt-<stem> --tb=short --durations=10 tests/<stem>.py`

Each row below substitutes its file stem into that command; every worker result exited 0 and cleanup exited 0. Logs are `.tmp/KBS-F1/current-<stem>.log`, except the corrected gates log is `current-test_kb_sync_gates-fixed.log`. Classifications: **U** unit; **D** labelled mock_contract / CPU HTTP double; **P** property. These checks establish plumbing and refusal behavior, with no inference or model quality claim.

| File | Class | Exact output |
|---|---|---|
| `test_kb_sync_gates.py` (corrected) | U/D | `19 passed in 2390.88s (0:39:50)` |
| `test_kb_sync_invalidation.py` | U/D | `26 passed in 2314.70s (0:38:34)` |
| `test_k2_probe.py` (final) | U/D | `18 passed in 17.51s` |
| `test_model_client.py` (final) | U/D | `78 passed in 29.48s` |
| `test_model_composition.py` | U/D | `54 passed in 86.76s (0:01:26)` |
| `test_k2_naming.py` | U/D | `24 passed in 41.44s` |
| `test_k2_observe.py` | U/D | `36 passed in 32.54s` |
| `test_k3_cards.py` | U/D | `19 passed in 39.16s` |
| `test_k4_aliases.py` | U/D | `41 passed in 23.27s` |
| `test_k2_config.py` | U/D | `310 passed in 164.92s (0:02:44)` |
| `test_k2_observe_config.py` | U/D | `186 passed in 44.29s` |
| `test_k3_config.py` | U/D | `279 passed in 104.79s (0:01:44)` |
| `test_k4_config.py` | U/D | `273 passed in 64.13s (0:01:04)` |
| `test_profiles.py` | U/D | `177 passed, 2 skipped in 43.77s` |
| `test_field_contracts.py` | U | `499 passed, 33 skipped in 199.72s (0:03:19)` |
| `test_storage_anchored_writes.py` | U/P | `156 passed, 5 skipped in 8.96s` |
| `test_model_production_probes.py` (final unmodified scratch baseline) | U/D/P | `53 passed in 14.13s` |
| `test_kb_sync_production_probes.py` (final unmodified scratch baseline) | D, actual production CLI | `1 passed in 4.18s` |
| `test_model_image.py` (inherited alpha-policy audit) | U/P | `202 passed, 4 warnings in 0.61s` |

The final three runs prepended `.tmp/KBS-F1/mutation` to `PYTHONPATH`; that unmodified scratch package contained only an import shim appending the original source package. Its inspected contents were `__init__.py` only, before any mutation. The image warnings are the existing Pillow I-mode PNG deprecation at `model_image.py:162`; no runtime change was made for them. The image file was added to verification to complete the alpha-policy effect/refusal audit, not to broaden implementation scope.

| Other evidence / command | Actual output and disposition |
|---|---|
| Unchanged-runtime production entry (red first), limiter pytest on `tests/test_kb_sync_production_probes.py` | `1 failed in 12.45s`; `.tmp/KBS-F1/red.log`; historical baseline failure |
| Earlier completed `tests/test_kb_sync_diagnostics.py`, limiter pytest, one file/process | `10 passed in 134.97s (0:02:14)`; `.tmp/KBS-F1/final-test_kb_sync_diagnostics.log`; no later change to that test or its runtime dependencies |
| PM-owned `tests/test_kb_sync_flow.py` | `28 passed in 3945.54s (1:05:45)`; main-project `.tmp/pm-checks/kbsf1-flow-pm.log`, read only; command not run by this worker in this continuation |
| `VP -B .tmp/KBS-F1/final_checks.py` | `syntax: 14 changed Python files parsed`; `mutation integrity: 8 original source hashes unchanged (text-normalized SHA-256)`; `scratch: no owned basetemp, mutation or Hypothesis trees remain`; `ruff_available=False`; `.tmp/KBS-F1/final-static.log` |
| `git diff --check -- src tests contracts tasks/active/KBS-F1-production-probes.md` | exit 0, no output; this checks tracked whitespace, not untracked files |
| `VP -B -m ruff check <all 14 changed Python files>`; `VP -B -m ruff format --check <same files>` (earlier actual attempts) | Both exit 1 with exact diagnostic suffix `python.exe: No module named ruff` (machine-specific prefix omitted). Lint and format are **not_run**, not passing; no install attempted. |

The artifact's final raw identity is size `1501`, SHA-256 `99d84e9f0b4ba1c749b3bb51ed6423f5a6a134bea834fe6a1ac8174bee7f4449`. Its independently pinned LF/CRLF encodings, nested validation, escaping-link refusal and byte-mutation properties passed in P. Command/result and cleanup boundaries are also retained in `current-command-results.txt` and `third-timeline.jsonl`.

### Scratch mutation report

Each mutant was prepared through `VP -B tools/agent-workflow/test_slot.py -- VP -B .tmp/KBS-F1/prepare_mutant.py <name>` (exit 0). Only its named module was copied; no tree copy or working-tree source mutation occurred. The common test command was the focused command above without `--durations=10`, with scratch first on `PYTHONPATH`, and appended `-k '<selection>'`. Each test file used its own fixed basetemp and was cleaned before the next run. Every failure below was a behavioral assertion, not a collection/import failure. Logs are `.tmp/KBS-F1/mutant-<name>.log`; exact source/mutant text-normalized SHA-256 and selections are in `mutation-manifest.jsonl`.

| Name / module | Changed behavior | File / exact `-k` selection | Actual output | Outcome / evidence |
|---|---|---|---|---|
| hash / `model_probe.py` | Ignore raw SHA, retain size | `test_model_production_probes.py` / `independent_bundle_pins and hash` | `1 failed, 51 deselected in 0.78s` | killed: expected `ProbeArtifactError` not raised |
| image-pin / `model_probe.py` | Skip embedded size/hash check | same file / `independent_bundle_pins and image_size` | `1 failed, 52 deselected in 0.66s` | killed: expected `ProbeArtifactError` not raised |
| answer / `model_probe.py` | Accept wrong integer answer | same file / `wrong_probe_answer` | `1 failed, 46 deselected in 0.59s` | killed: wrong answer 8 accepted as true |
| allowance / `model_probe.py` | Skip remaining allowance debit | same file / `retries_are_actual_sends` | `1 failed, 51 deselected in 1.11s` | killed: expected `ProbeLimitExhausted` not raised |
| cloud-release / `model_transport.py` | Skip unsent cloud-reservation release | same file / `retries_are_actual_sends` | `1 failed, 1 passed, 51 deselected in 1.39s` | killed: remaining cloud grant 2 instead of 3 |
| safe-code / `model_client.py` | Return arbitrary failure value | same file / `structural_failure_code_property` | `1 failed, 52 deselected in 0.91s` | killed: None outside closed codes; list unhashable |
| combination / `model_client.py` | Skip image/output combination demand | same file / `image_plus_schema` | `1 failed, 52 deselected in 1.11s` | killed: expected `ModelCallError` not raised |
| shared-run / `kb_sync.py` | Skip coordinator-owned run, leaving standalone stage runs | `test_kb_sync_production_probes.py` / `kb_sync_cli_probes_without_injection` | `1 failed in 5.62s` | killed: actual 18 probe sends instead of 8 |

**8 killed, 0 survived, 0 not_run.** Each pytest exit was 1 and cleanup exit was 0 (`mutant-results.txt`). The final original-source hash comparison passed for all eight manifest rows. The shared-run mutation targets the newly added coordinator ownership condition; its acceptance test is the production-entry file, never the PM-owned flow file.

### Configuration audit and remaining work

Every row of **Configuration audit (continuation)** above is now backed by executed results: P 53, M 54, F 499/33 skipped, K 310, observe/K3/K4 configuration 186/279/273, model client 78, image 202, profiles 177/2 skipped, diagnostics 10, gates 19, storage guard 156/5 skipped, and PM flow 28. Operational rows are validated existing settings with override/effect/refusal tests; artifact pins, challenge protocol, safe code set, structural evidence and ownership checks retain the classes stated in the row table. No operational literal is left classified as defect or awaiting a new configuration key. Retained 0/1/2 counters, retry debit, image pair cardinality, JSON/schema keys and closed role/capability/status fields are protocol/safety/artifact bounds, not deployment choices. Example design sentinels were not fitted or replaced by this task.

Open verification: Ruff lint/format **not_run** (missing tool; PM explicitly forbade installation). Full suite **not_run** (PM-owned). Live model, GPU, cloud and quality checks **not_run** (outside permission/scope); CPU doubles are labelled. Existing skipped counts and Pillow warnings are reported above. Independent Claude review and integration remain PM-owned. No code blocker or surviving mutant remains in this focused handoff.

### Cleanup and lessons learned

Owned test basetemps, the stale flow basetemp, mutation package, Hypothesis state and temporary scripts were removed; small cited logs/manifests/reports remain under `.tmp/KBS-F1/`. No worker test process, loopback server or thread remains running: every direct command returned, server fixtures joined in `finally`, and recorded mutant shells were checked after completion. No PM-owned basetemp or evidence tree was removed. `VP -B .../prepare_mutant.py clean` printed `mutation_exists=False`; each `clean_bt.py <owned-name>` printed `owned_basetemp_exists=False`. Final cleanup verification is retained in `final-cleanup.log`.

Lessons for PM: an outer launcher timeout must never authorize cleanup while its descendant may still use a basetemp. Prefer direct limiter execution, one fixed tree per file and verified exit before cleanup. Trace capability failures from run-owned structural evidence before changing demand/verifier/accounting. An expected negative proof, an unsent exhausted allowance and a failed request require distinct codes. A test read-set inventory must follow its command's actual operational reads; whole-file schema constraints still apply. These are worker notes for PM transfer, not edits to PM-owned lesson/decision files.

## Initial implementation Worker completion (historical)

- [x] Changes: production pinned probes, shared lazy run, actual-send/cloud accounting and safe failure evidence implemented; test-only query-template inventory correction recorded; R-015/R-017/R-018/R-020/R-026 covered.
- [x] Checks: all requested focused files passed, final CLI exact-eight regression passed, PM flow 28 passed, eight scratch mutants killed; syntax/source-integrity/whitespace checked. Ruff not_run because absent, as instructed.
- [x] Cleanup: owned basetemps, mutation/Hypothesis trees and temporary scripts removed; small evidence reports retained; every worker command finished and owned loopback fixtures joined.
- [x] Remaining: independent Claude review and PM full-suite/integration; Ruff lint/format unavailable here. No live quality claim, Git write or external/model/GPU call made.

## Initial implementation Handoff (historical)

- **Status / already done:** REVIEWING. Approved implementation and focused verification complete, with the explicit not_run items above. The final output table supersedes historical stopped-attempt reports; preserve red/diagnosis/history as evidence.
- **Next agent:** independently review the 9 changed runtime files, versioned artifact, 5 changed/new test files and this task file; inspect class-sweep/S2 requirements and the audit rows. PM owns full-suite execution, decision transfer and Git integration.
- **Must not repeat:** no shared basetemp across live sessions, custom 600-second launcher, basetemp cleanup before exit, invented mutant/lint pass, full suite in worker scope, model/GPU/cloud spend, cap increase, artifact hand-copy or secret access.
- **Noticed, not done:** unrelated sampler-thread smoke finding and stale dev-machine residency prose remain outside scope. Existing Pillow I-mode PNG deprecation is reported without an unrelated fix. No T-014 implementation file, PM-owned status/decision file or production server lifecycle was changed.

## Completion report

**Fix round 1 — REVIEWING (R-015, R-018, R-020, R-026).** Small-task Step 0, research and pre-edit Configuration impact are at the top of this file. F1-F4 are addressed and the requested focused checks passed. Six scratch mutants were killed. Stop for independent Claude retest; this is an implementer handoff, not a self-review or integration approval.

### Work completed / finding dispositions

| Finding | Change and evidence |
|---|---|
| F1 | `src/prompt_enhancer/model_client.py`: clear an ordinary/ordered in-flight mark when an exception escapes before evidence; preserve observed proof and spent sends. Initial and lazy production probes now reach the same existing budget handler. Bind/call expose safe budget/incomplete codes and evidence. `model_transport.py`: release an unsent reservation for BaseException, including cancellation. Red actual-HTTP regression first reproduced CAPABILITY_UNAVAILABLE; final production-proof file passes 75 tests, covering fail/fallback, grants 0/1/2/3, binding/lazy/generation and the exception sweep. |
| F2 | `tests/test_k3_cards.py` and `tests/test_k4_aliases.py`: three cases each prove persisted negative, failed and incomplete probe evidence before generation, using real anchored checkpoint commits and a fresh writer. Assert role, code/status, no call record, bounded probe sends, no card/alias and no private error text. No K3/K4 runtime change in this round. |
| F3 | `model_client.py`: all three cloud-header consumers use one lazy cache per backend in the run-owned RoleClient; close clears it in finally. Header-count test covers repeated binding, generation, lazy image extension and a fresh run. A labelled credential-loader double avoids reading any real secret; no credential/header is logged or persisted. Existing provider/transport configuration remains authoritative. |
| F4 | `docs/rules/VLM_CALL_RULES.md:6` and this task: text/JSON probes demonstrate instruction following through strict deterministic replies. They do not establish response_format enforcement or constrained decoding. Post-engine schema validation is still required. No verifier change for F4. |

Fix-round tracked files: the two runtime files above, the rule document, the three test files above and this task file. No new configuration key, schema/profile edit, capability table, natural-language content rule or production test double. No server lifecycle change or T-014 implementation edit. The exception/credential sweep above records every required boundary, mark disposition, code/policy and reservation outcome.

### Actual commands and results

All commands ran from the worktree root. `VP` means project venv `../../../.venv/Scripts/python.exe`; required environment was `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`. Hypothesis state stayed in `.tmp/KBS-F1/fix1-hypothesis`. All pytest commands used the limiter, one file per process, serially, with faulthandler and no xdist/full suite. Each per-file basetemp was removed after exit; cleanup exits were 0 (`fix1-results.txt`).

Common focused command **C(stem)**:

`VP -B tools/agent-workflow/test_slot.py -- VP -B -X faulthandler -m pytest -q -x -p no:cacheprovider --basetemp=.tmp/KBS-F1/bt-<stem> --tb=short --durations=5 tests/<stem>.py`

Each row substitutes its stem. Logs are `.tmp/KBS-F1/fix1-<stem>.log`; all are unit / labelled CPU mock_contract checks, with property tests where noted. No inference or model-quality evidence is claimed.

| Command / file | Actual summary line |
|---|---|
| C(`test_model_production_probes`) before final test cleanup/additions | `73 passed in 27.67s` |
| C(`test_kb_sync_production_probes`) | `1 passed in 3.64s` |
| C(`test_model_client`) | `78 passed in 11.00s` |
| C(`test_model_composition`) | `54 passed in 61.32s (0:01:01)` |
| C(`test_k2_probe`) | `18 passed in 3.48s` |
| C(`test_k3_cards`) | `22 passed in 39.76s` |
| C(`test_k4_aliases`) | `44 passed in 26.01s` |
| C(`test_kb_sync_diagnostics`) | `10 passed in 19.05s` |
| C(`test_kb_sync_gates`) | `19 passed in 74.60s (0:01:14)` |
| C(`test_field_contracts`) | `499 passed, 33 skipped in 179.44s (0:02:59)` |
| C(`test_storage_anchored_writes`) | `156 passed, 5 skipped in 8.88s` |
| Final unmodified scratch baseline: C(`test_model_production_probes`) with scratch prepended to PYTHONPATH | `75 passed in 43.25s`; `.tmp/KBS-F1/fix1-production-final.log`; final state supersedes the 73-case run |

Final baseline used `.tmp/KBS-F1/fix1-mutation` before the original src directory on PYTHONPATH; its package was an unmodified import shim, prepared by the helper's `baseline` action. The final 75-case file retains one complete credential-count test and adds both escaping-allowance entry cases. Other tested runtime files were unchanged after the serial run.

| Other command / experiment | Actual output / disposition |
|---|---|
| C(`test_model_production_probes`) with `-k production_cloud_probe_budget_falls_back_over_http`, before runtime edits | `1 failed, 53 deselected in 2.34s`; `CAPABILITY_UNAVAILABLE: required capability is unproved`; `fix1-red.log` |
| First expanded production-proof file | `1 failed, 67 passed in 51.47s`; explained fixture deadline issue above. Original per-file log was overwritten; actual tool-output summary is preserved honestly in `fix1-property-first-summary.txt`. No full-traceback retention claim. |
| `VP -B -I tools/agent-workflow/recall.py build` | `indexed=0 unchanged=94 removed=0 refused=0 chunks=0 skipped_chunks=0` |
| Same interpreter/flags, `recall.py search 'probe cloud budget exhaustion fallback credentials' --limit 5` | exit 0, no hits/output |
| `VP -B tools/agent-workflow/test_slot.py -- VP -B .tmp/KBS-F1/fix1_prepare_mutant.py --help` | exit 0; `usage: fix1_prepare_mutant.py [-h] {baseline,rollback,lazy-policy,headers,reservation-cancel,cancel-rollback,ordered-rollback}` |
| Same helper command with `baseline` or each mutant name | exit 0; `prepared=baseline` or `prepared=<name>` |
| `VP -B tools/agent-workflow/test_slot.py -- VP -B .tmp/KBS-F1/fix1_static.py` | `syntax: 5 fix-round Python files parsed; no duplicate top-level test definitions`; `mutation integrity: 6 original source hashes unchanged (text-normalized SHA-256)`; `installed asyncio: CancelledError is a BaseException, not an Exception`; `ruff_available=False`; `fix1-static.log` |
| `git diff --check -- src tests docs/rules/VLM_CALL_RULES.md tasks/active/KBS-F1-production-probes.md` | exit 0, no output; tracked-whitespace check only |
| Ruff lint / `ruff format --check` | **not_run** this round: Ruff absent from the project venv, installation prohibited. Prior actual attempts reported exact suffix `python.exe: No module named ruff`; no lint/format pass claimed. |

The final requested files total 976 passed and 38 skipped across separate sessions (75-case final production-proof result used once). Gates' inventory cases dominated its 74.60s; field-contract inventory dominated its 179.44s. Per-test durations are retained in each log. The PM-owned flow file and full suite are not_run by this worker in this round; prior flow evidence is historical and is not claimed to retest this fix.

### Changed-line mutants

Each scratch mutation copied only its named module, preserving the original package through an import shim. Preparation command is above. Test command: C(`test_model_production_probes`) without `--durations=5`, with scratch first on PYTHONPATH and appended `-k '<selection>'`. Every command returned pytest exit 1 because of the listed behavioral assertion, with cleanup exit 0; none was a collection/import failure. Logs: `.tmp/KBS-F1/fix1-mutant-<name>.log`; provenance and exact text-normalized source/mutant hashes: `fix1-mutation-manifest.jsonl`.

| Mutant / module | Change / exact selection | Actual summary | Outcome |
|---|---|---|---|
| rollback / model_client | Skip `attempted.pop`; `production_cloud_probe_budget_falls_back_over_http` | `1 failed, 74 deselected in 2.34s` | killed: CAPABILITY_UNAVAILABLE replaces fallback |
| lazy-policy / model_client | Refuse fallback when earlier proof exists; `production_budget_policy_property` | `1 failed, 74 deselected in 25.35s` | killed: grant 1 / lazy / fallback policy raises budget error |
| headers / model_client | Reload cached headers; `cloud_headers_loaded_once_per_backend_per_run` | `1 failed, 74 deselected in 1.47s` | killed: five loader calls instead of one |
| reservation-cancel / model_transport | Catch Exception instead of BaseException before send; `unsent_probe_reservation_released_for_every_escape` | `1 failed, 1 passed, 72 deselected in 2.08s` | killed: unspent grant remains 4 instead of 5 on cancellation |
| cancel-rollback / model_client | Roll back only budget/allowance exceptions; `production_probe_exception_property` | `1 failed, 74 deselected in 17.06s` | killed: cancelled text remains marked incomplete |
| ordered-rollback / model_client | Skip ordered-mark discard; `cancelled_ordered_probe_does_not_poison_extension` | `1 failed, 74 deselected in 1.51s` | killed: cancelled ordered extension stays marked |

**6 killed, 0 survived, 0 not_run.** Final source hash comparison confirms that all six original-source manifest rows remained unchanged; no working-tree source was mutated. `fix1-mutant-results.txt` records test/cleanup exits.

### Configuration audit

No new field or operational default. Schema owner remains `config.py` plus `contracts/schemas/enhancer_config.v0.schema.json`; base/dev example declarations remain unchanged. Previous inherited image/output/HTTP audit rows still apply; this round reran composition, client, probe, gate and both guard files. Here **P** means final 75-case `test_model_production_probes.py`, **M** model composition, **F** field contracts. Every test reference below has a passing file result above.

| Operational value / behavior | Validated configuration path | Validation | Example/profile | Override/effect and refusal evidence | Retained-literal class |
|---|---|---|---|---|---|
| Exhaustion policy | `vlm.backends.*.on_budget_exhausted` | Existing closed `stop` / `fallback_to_default_backend` enum; unresolved/missing refused | Existing base/dev | P `test_production_budget_policy_property` exercises both loaded policies through binding/lazy/generation; `test_production_budget_configuration_refusal[on_budget_exhausted]`; F same key | configurable |
| User grant | `vlm.backends.*.call_budget` | Required nonnegative exact integer, sentinel refused before calls | Existing base/dev | P policy property grants 0/1/2/3 observes backend, warning and ledger remaining; configuration refusal `[call_budget]`; cloud retry tests | configurable; debit safety-bound |
| Local default | `vlm.default_backend` | Existing declared local backend validation, no fallback to exhausted backend | Existing base/dev | P actual loopback fallback and policy property; M cloud-sharing tests and F same path; gates missing/type/sentinel refusal | configurable |
| Actual probe cap | `vlm.backends.*.probe.max_requests` | Existing positive exact integer; no reset/increase | Existing base/dev | P allowance override/lazy/retry tests and exception property; escaping allowance tests at bind/call; per-key missing/sentinel/invalid refusal | configurable; counter safety-bound |
| Probe deadline | `vlm.backends.*.probe.timeout_s` | Existing finite positive validation | Existing base/dev | P timeout effect/refusal and exception property; short deadline only for deliberate timeout | configurable; fixture deadlines test-only |
| Retries / transport deadlines | `vlm.backends.*.http.*` | Existing explicit retry/timeouts; cancellation does not refund committed sends | Existing base/dev | P retries/actual sends, exception property and unsent reservation classes; M per-key effect tests; F/gates refusal | configurable; reservation ordering safety-bound |
| Header content and routing | `vlm.backends.*.{provider,api_key_file,anthropic_version,anthropic_workspace_id}` | Existing provider/path/sentinel validation; all consumers share one backend cache | Existing base/dev | P header-count and production cloud tests use labelled loader; existing model-client cloud key/provider/media tests and M loaded cloud profile; F/gates validation | configurable; provider header names protocol-bound |
| Header lifetime | Run-owned cache, no new key | Lazy per-backend load, close clears cache, new run loads again | No profile addition | P `test_cloud_headers_loaded_once_per_backend_per_run` observes load count across binds/calls/extensions/two runs and empty cache on close; unsafe/missing configuration still refused before use | safety-bound |
| Attempt lifecycle / safe failure code | Closed existing codes and probe evidence | Unobserved escaping attempt is cleared; completed failed/incomplete evidence retained; no refusal converted to negative proof | No key | P budget/exception properties, escaping refusal and cancelled ordered extension; K3/K4 durable evidence and diagnostics; guards passed | protocol-bound; state/evidence safety-bound |
| Strict verifier scope | Existing challenge contract, no key | Instruction following only; no new assertion of constrained decoding | Rule 2 clarified | Existing strict verifier/artifact properties still passed in P; no F4 runtime edit | protocol-bound / artifact-bound |

All new empty containers/counter-state transitions are run ownership/safety invariants; closed error/capability/status/header names are protocol-bound. Synthetic grants, deadlines, routes, loader answers and ports are test-only. Artifact identity/pins and verifier arithmetic did not change. No unclassified operational literal, ignored configured value or new hidden deployment default remains in this fix.

### Cleanup, remaining work and lessons

Owned file-specific basetemps were removed after their process exited (`bt-<stem>: exists=False`). Removed the fix1 mutation package and Hypothesis tree (`fix1-mutation: exists=False`, `fix1-hypothesis: exists=False`) and the three iteration scripts. Small cited logs, result tables, static evidence and mutation manifest remain under `.tmp/KBS-F1/`; the reviewer's `scratch/test_scratch_fallback.py` and review report were left untouched. Final cleanup/whitespace evidence is `fix1-cleanup.log`. All direct limiter commands returned and the loopback fixture joined its thread; no owned test, server or subprocess remains running.

Ruff lint/format remain not_run because unavailable; PM flow/full suite and Claude retest remain PM-owned. No live inference, GPU, cloud spend, external network, install or Git write occurred. F4 explicitly limits what probe success establishes; no model quality claim is made.

Lessons: a mark set before await is not evidence when the await raises. Exercise zero grant and partial proof through production composition, not only the transport seam. Cancellation derives from BaseException in the installed Python, so pre-send reservation cleanup must include it. Give deliberate exception tests enough configured time to reach their boundary, with short deadlines reserved for timeout tests. Preserve per-attempt failing logs under distinct names; this round's overwritten first property log is explicitly a transcribed summary. PM transfers decisions/lessons; worker does not edit shared decision/status files.

Final cleanup verification (`.tmp/KBS-F1/fix1-cleanup.log`): `owned_basetemp_count=0`, `mutation_exists=False`, `hypothesis_exists=False`, `iteration_script_count=0`, `cleaner_exists=False`, `git_diff_check_exit=0`. All direct test/mutant/static commands returned; owned loopback fixtures joined. Reviewer scratch and PM evidence were retained. No owned process remains running.

## Worker completion

- [x] Changes: F1 budget/allowance/cancellation state and policy corrected; F2 K3/K4 evidence tests added; F3 headers cached per backend/run; F4 verifier scope documented. Configuration impact and audit completed.
- [x] Checks: final production probes 75 passed; all requested focused files passed; six scratch mutants killed; syntax, source-integrity and whitespace checked. Ruff lint/format not_run because unavailable.
- [x] Cleanup: owned basetemps, mutation/Hypothesis trees and iteration scripts removed; small evidence retained; all owned commands finished and loopback thread joined. Reviewer scratch and PM evidence untouched.
- [x] Remaining: independent Claude retest, PM flow/full-suite verification and integration. No self-review, live-quality certification or Git write.

## Handoff

- **Current status / already done:** REVIEWING; fix-round report and final outputs above supersede the initial implementation handoff. The red production-budget failure is preserved; focused regressions and properties now pass.
- **Next:** Claude retests F1-F4, exception/credential sweep, audit and six mutants; PM runs flow/full suite and handles decision transfer/integration.
- **Must not repeat:** no worker flow/full suite, shared live basetemp, custom outer-timeout launcher, cleanup before exit, credential/header logging, cap increase, model/GPU/cloud spend or Git writes.
- **Noticed, not done:** unrelated sampler-thread/residency findings and existing Pillow warning remain outside scope. No additional runtime/API or provider-proof expansion was attempted.

## Integration fix round 1 (INT-F1)

**Status: REVIEWING.** Small: yes, so Step 0 sits atop and the work continued. R-015, R-018 (R-017 untouched). Codex retest and PM full suite/flow remain PM-owned.

### Step 0

- **Unknowns:** (1) which probe sites build image bytes and which side each applies; (2) whether the pair should also take the role's format and quality, not only the side; (3) what a pair that cannot be prepared must report. No question needs the user: the rule and the codes already exist (decision 144; `PROBE_FAILED`, `INPUT_INVALID`).
- **Answers taken (PM decision pending user review):** (2) yes, one shared preparation (side, format, quality, alpha, decode bound), so the proof uses the bytes generation would send; (3) a failed `multiple_images` event with the existing `INPUT_INVALID` reason and `PROBE_FAILED` error, no send, proof absent, never retried. No cap is raised anywhere; a cap that makes a challenge unanswerable gives an honest negative, not a hidden pass.
- **Plan and finish:** share `image_max_side` with both builders; promote the reproducer; property, loaded-profile, fallback and failure tests; serial runs; 6 mutants; audit rows. Finish = reproducer green, listed files green, mutants killed.

### Class sweep (before the edit; "after" is the state now)

| # | Site (file:line before the edit) | Side applied before | Side it must apply | After |
|---|---|---|---|---|
| 1 | `model_probe.production_probe_cases`, single `image` challenges (`model_probe.py:141-148`, two images) | role side or `from_vision`; backend side ignored | `image_max_side(config, role['image'], backend)` | `max_side` argument computed by `RoleClient._production_probe`, preparation in the shared `prepare_probe_image` |
| 2 | same function, `image+json_object` / `image+json_schema` combined challenges (same lines, two images each) | same as 1 | same | same code path as 1 |
| 3 | `model_probe.multiple_images_plan` (`:294-314`), built by `RoleClient._multiple_images_pair` (`model_client.py:306`), sent by `_extend_multiple_images` / `probe_local_multiple_images_pair` (two images per request, two requests) | none: raw 96x64 PNGs | same as 1, for the role's backend | `_multiple_images_pair` passes a `prepare` closure over `prepare_probe_image` with `image_max_side`; plan builds data URLs from its media type |
| 4 | fallback backend: `_call` binds `_production_probe(fallback, role=role)` (`model_client.py:619`) and `start_backend` | role side only | the fallback backend's own side | `image_max_side(..., backend)` receives the effective backend, tested with a cloud role falling back to a local backend with side 32 |
| 5 | `_start_backend` local-plan path (`local_probe_plans`, `probe_local_image_pair`/`thinking_pair`, `model_client.py:~479-512`) | caller-built bodies | n/a in production | Not reachable in production: `_production = probe_cases is None and local_probe_plans is None and not probes` (`model_client.py:277`); no `src/` caller passes `local_probe_plans` (`kb_sync.py`, K2 to K4 stages forward only their `None` defaults). Plans are labelled test seams whose images the test builds. Its `multiple_images` entry falls back to `production_pair`, which is sized (row 3) |
| 6 | K2 to K4, `kb sync` probe paths | build no image bytes | none | Only `ModelClientRun`/`RoleClient` build probe images; `grep` of `src/` for `prepare_image`, `multiple_images_plan`, `production_probe_cases` finds only `model_probe.py` and `model_client.py` besides the definition in `model_image.py` |
| 7 | `probe_local_multiple_images_pair` verifier and `ProbeResult` | none | none | unchanged: it checks the pair differs only by order, which still holds after sizing |

One shared rule: `model_client.image_max_side` (generation) is called by the probe paths; `model_probe.prepare_probe_image` is the single preparation for single and ordered probe images (it needs no second copy of the rule because it receives the resolved side).

### Work completed

- `src/prompt_enhancer/model_probe.py`: new `prepare_probe_image(data, max_side, backend, role, vision)` (media type refused if unregistered); `production_probe_cases(..., capability, max_side)`; `multiple_images_plan(model_id, max_tokens, prepare)`; the unused inline preparation is removed; `image_media_type` imported at module level (no import cycle: `config.py` imports neither probe nor client).
- `src/prompt_enhancer/model_client.py`: `_production_probe` computes `image_max_side` for image capabilities before the in-flight mark (an invalid side is refused like generation, no leaked mark); `_multiple_images_pair` computes it too and, if the fixture cannot be prepared (for example `max_decode_pixels` below 96x64), records a failed `multiple_images` event (`INPUT_INVALID`), `PROBE_FAILED`, marks the pair attempted, sends nothing; a repeated bind does not repeat it.
- Tests: `tests/test_model_production_probes.py` +9 cases (property over role side explicit / `from_vision` / `none`, vision side and backend side `none` / below 96 / above 96 across the single, image+schema and ordered-pair probes; five loaded-profile loopback cases; invalid backend side refusal; fallback backend; unpreparable pair); two existing call sites updated for the new arguments (`tests/test_model_production_probes.py`, `tests/test_k2_probe.py`). `tests/test_pipeline_s0_s3.py` and `tests/conftest.py` untouched. No schema, profile or example key changed.

### Actual commands and results

All from the worktree root with the project venv, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`, Hypothesis storage under `.tmp/KBS-F1-int/`. Each command: `VP -B tools/agent-workflow/test_slot.py -- VP -B -X faulthandler -m pytest -q -x -p no:cacheprovider --basetemp=.tmp/KBS-F1-int/bt-<stem> --tb=short tests/<stem>.py`, one file per process, serial, basetemp removed after exit (a scratch runner issued them; logs `.tmp/KBS-F1-int/fix1-<stem>.log`). Unit, property and labelled loopback/in-process doubles; no inference.

| File | Result |
|---|---|
| `test_model_production_probes.py` | `84 passed in 76.83s (0:01:16)` (was 75; +9) |
| `test_kb_sync_production_probes.py` | `1 passed in 4.61s` |
| `test_model_client_image_side.py` | `36 passed in 8.91s` |
| `test_model_client.py` | `79 passed in 19.10s` |
| `test_k2_probe.py` | `18 passed in 6.07s` |
| `test_model_composition.py` | `54 passed in 83.54s (0:01:23)` |
| `test_pipeline_s0_s3.py` | `1 passed in 6.39s` |
| `test_k2_fix_round3.py` | `35 passed in 53.43s` |
| `test_field_contracts.py` | `502 passed, 34 skipped in 234.44s (0:03:54)` |
| `test_storage_anchored_writes.py` | `159 passed, 5 skipped in 10.11s` |
| `test_property_settings.py` | `1 passed in 3.85s` |
| Reviewer's reproducer `.tmp/KBS-F1-int/test_review_cap.py` (review command: `-p conftest`, `PYTHONPATH` with `tests` and the scratch directory) | `2 passed in 2.32s` (was `2 failed in 2.39s`); `fix1-repro.log` |

`ruff --version` gave `No module named ruff`: lint and format `not_run`, no install. `git diff --check -- src tests`: exit 0. `test_kb_sync_flow.py` and the full suite: not run (PM-owned).

### Mutants (scratch copy of one module in a shim package, working tree untouched; the changed-line test selection of `test_model_production_probes.py`)

| Mutant (module) | Change | Result |
|---|---|---|
| probe-side-dropped (`model_client`) | pass `'none'` as the single/combined probe side | killed: `1 failed in 316.87s` (Hypothesis shrink), `(96, 64) == (1, 1)` |
| pair-unsized (`model_client`) | ordered pair prepared as passthrough | killed: `1 failed in 2.57s`, `(96, 64) == (1, 1)` |
| role-side-only (`model_client`) | pair side ignores the backend cap | killed: `1 failed in 2.21s` |
| helper-ignores-side (`model_probe`) | `prepare_probe_image` passes `'none'` | killed: `1 failed in 2.57s` |
| fallback-side-from-role-backend (`model_client`) | side from the role's own backend, not the effective one | killed: `1 failed in 1.49s`, `[(96, 64)] == [(32, 21)]` |
| pair-failure-silent (`model_client`) | do not mark the unpreparable pair attempted | killed: `1 failed in 1.81s`, event recorded twice |

6 killed, 0 survived, 0 not_run.

### Configuration audit (rows for this fix; no new key)

| Behavior | Validated path | Validation | Example/profile | Override/effect and refusal tests | Class |
|---|---|---|---|---|---|
| Backend sent side | `vlm.backends.*.image.max_side` | schema positive int or `none` (existing); a non-limit refused by `image_max_side` before any probe mark | base/dev examples keep `none`, unchanged | effect: property (backend `none`, 1 to 48, 97 to 200), loaded loopback cases 32 and 48, fallback case 32, in `test_model_production_probes.py`; refusal: `test_invalid_backend_side_is_refused_without_a_probe_mark`, existing `test_image_max_side_refuses_a_non_limit`, `test_field_contracts.py` | configurable |
| Role sent side | `vlm.roles.*.image.max_side` (`from_vision` or int or `none`) | same helper; `from_vision` resolved from vision | unchanged | effect: property over explicit, `from_vision`, `none`; loaded case role 20 below backend 32; refusal: existing `test_field_contracts.py` and `test_model_composition.py` unresolved `from_vision` | configurable |
| Inherited side | `vision.max_image_side` | same helper | unchanged | effect: property and loaded case vision 24 below backend 32; refusal: existing field contracts and composition tests | configurable |
| Probe image format/quality/alpha/decode bound | role `image.{format,quality}`, `vision.{format,alpha_policy}`, backend `image.max_decode_pixels` | one shared preparation now also used by the ordered pair; decode bound never raised | unchanged | unpreparable-pair test (bound 1000 below 96x64 gives failed event, no send); existing single-image tests; refusal: existing | configurable |
| Fixture geometry (96x64, answers 12/21, artifact pins) | none | unchanged | none | unchanged | artifact-bound / protocol-bound |
| Test sides, ports, probe caps in the new tests | tests only | n/a | n/a | n/a | test-only |

No unclassified literal, ignored setting or hidden default was added; the one new parameter (`max_side`, `prepare`) is required, with no default.

### Cleanup, remaining work, lessons

Removed: scratch runners, the mutation harness, the shim package, Hypothesis state and every `bt-*` basetemp (verified absent); cited logs `fix1-*.log` and the reviewer's files remain under `.tmp/KBS-F1-int/`. No process, server or thread left running; loopback servers joined by the fixture. No Git write, install, model call or `.secrets/` access. Remaining: Codex retest, PM flow/full suite. **Noticed, not done:** Ruff is absent from the venv. **Lesson:** a probe that builds its own input must take the same resolved parameters as generation; vary every shared operational field (here the backend side) on each newly reachable send path, not only the defaults.

## Worker completion (integration fix round 1)

- [x] Changes: INT-F1 closed for single, combined, ordered-pair and fallback probes through one shared rule.
- [x] Checks: 11 listed files and the promoted reproducer passed; 6 of 6 mutants killed; Ruff not_run (absent).
- [x] Cleanup: scratch, basetemps and Hypothesis state removed; cited logs kept.
- [x] Remaining: independent Codex retest and PM flow/full suite.

## Integration fix round 2

**Status: REVIEWING.** Small: yes. R-015, R-018. Test-only change; no runtime, schema, profile or example edit.

### Step 0

- **Unknown:** is the per-capability summary code in `ProbeResult.errors` (`errors['multiple_images'] = 'PROBE_FAILED'`) intended, or a defect that should be removed from the runtime? No question for the user.
- **Decision (source-verified): intended; the test is stale.** At base `d8a8130`, `_extend_multiple_images` merged only the per-challenge errors (`git show d8a8130:src/prompt_enhancer/model_client.py`, lines 255-258), so the old test's `{'TimeoutError'}` was exact. KBS-F1's design requires incomplete, failed and negative proofs to stay distinct (Step 0 unknown 6 and 7, this file) and `RoleClient._call` derives its public code from `errors.get(capability)` (`model_client.py:653-656`: `PROBE_INCOMPLETE`, else `PROBE_FAILED`, else `CAPABILITY_UNAVAILABLE`); without the summary key a failed ordered proof would be reported as `CAPABILITY_UNAVAILABLE`. `test_model_production_probes.py` already pins the key (`:242-243`, `:835`, `:1018`). Removing it would break that distinction, so the runtime stays.
- **Plan and finish:** sweep every reader of `ProbeResult.errors`; update only the stale assertion, keep all its claims, add the new one; run the listed files serially; 3 mutants.

### Class sweep: readers of `ProbeResult.errors` / `.errors.values()` (grep of `src/` and `tests/`)

Key spaces: the helper (`probe_capabilities`) writes errors keyed by challenge name (`first`, `second`, `0`, `1`, or an injected case name); the client adds summary keys named like capabilities (`text`, `image`, `multiple_images`, ...) only after a non-complete status. A summary key can therefore only be read where the code asks for a capability name.

| Reader | Does the added key change what it checks? | Action |
|---|---|---|
| `model_client.py:653` (`_call`, `.errors.get(cap)` for each missing capability) | It is the consumer of the summary key; it needs it | none |
| `model_client.py:423`, `model_probe.py:366` (`name in result.errors and ... != 'PROBE_INCOMPLETE'`, `name` is a challenge name) | no: the result they read comes from the helper before any summary key is added | none |
| `model_client.py:322, 415, 440, 456, 467, 516, 526`, `model_probe.py:273-274, 303-304, 369-370` (merge or forward `errors`) | Writers/copiers, no value inspected | none |
| `tests/test_k2_fix_round2.py:185` (`errors == {'prior': 'RecordedError'}`) | no: the stubbed pair completes, so no summary key is written (file passes) | none |
| `tests/test_k2_fix_round2.py:221` (`set(errors.values()) == {'TimeoutError'}`) | **yes**: the failed pair now also records `multiple_images: PROBE_FAILED` | updated |
| `tests/test_model_composition.py:296` (`errors.get('text')`, expects `TimeoutError`) | no: that run injects `probe_cases`, so `text` is a challenge name written by the helper, not a summary key | none |
| `tests/test_model_probe.py:109` (`errors['image'] == 'TimeoutError'`) | no: calls `probe_local_image_pair` directly, no client | none |
| `tests/test_model_production_probes.py:242, 243, 835, 1018` | already assert the summary keys | none |

### Work completed

`tests/test_k2_fix_round2.py::test_lazy_proof_uses_configured_deadline_without_retrying_unknown_result`: the line `set(errors.values()) == {'TimeoutError'}` became `errors.pop('multiple_images') == 'PROBE_FAILED'` followed by `errors == {'first': 'TimeoutError', 'second': 'TimeoutError'}` (stricter than before: both challenges named). The test's other claims are unchanged: exactly two sends (`len(calls) == 2`), no retry of an unknown result (third bind sends nothing), `status == 'failed'`, `multiple_images` proof absent.

### Actual commands and results

Red first: `VP -B tools/agent-workflow/test_slot.py -- VP -B -X faulthandler -m pytest -q -p no:cacheprovider --basetemp=.tmp/KBS-F1-int/bt-r2red --tb=short "tests/test_k2_fix_round2.py::test_lazy_proof_uses_configured_deadline_without_retrying_unknown_result"` gave `1 failed in 1.24s`, `Extra items in the left set: 'PROBE_FAILED'` (`fix2-red.log`), reproducing the PM's failure. Then each file below with the same command shape (`-x`, `--basetemp=.tmp/KBS-F1-int/bt-<stem>`, one file per process, serial, basetemp removed after exit; env `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<worktree>/src`; logs `.tmp/KBS-F1-int/fix2-<stem>.log`). Unit and labelled doubles, no inference.

| File | Result |
|---|---|
| `test_k2_fix_round1.py` | `39 passed in 28.00s` |
| `test_k2_fix_round2.py` | `42 passed in 59.12s` |
| `test_k2_fix_round3.py` | `35 passed in 49.24s` |
| `test_k2_probe.py` | `18 passed in 5.35s` |
| `test_model_production_probes.py` | `84 passed in 79.60s (0:01:19)` |
| `test_model_client.py` | `79 passed in 25.30s` |
| `test_kb_sync_production_probes.py` | `1 passed in 5.72s` |
| `test_k2_naming.py` | `24 passed in 31.97s` |
| `test_model_composition.py` (sweep reader) | `54 passed in 72.01s (0:01:12)` |
| `test_model_probe.py` (sweep reader) | `5 passed in 0.16s` |

`git diff --check -- tests`: exit 0. Ruff still absent: lint/format `not_run`. Full suite and flow file: not run (PM-owned).

### Mutants (scratch module copy in a shim package; changed test `-k lazy_proof_uses_configured_deadline`)

| Mutant | Change | Result |
|---|---|---|
| summary-code-dropped (`model_client`) | do not write `errors['multiple_images']` | killed: `1 failed in 1.36s`, `KeyError: 'multiple_images'` |
| summary-code-wrong (`model_client`) | write `PROBE_INCOMPLETE` instead of the status code | killed: `1 failed in 1.40s`, `'PROBE_INCOMPLETE' == 'PROBE_FAILED'` |
| challenge-error-lost (`model_probe`) | record `Other` instead of the exception name | killed: `1 failed in 1.21s`, per-challenge dict differs |

3 killed, 0 survived.

### Configuration audit

No operational value, key, default or literal added or changed (test-only). `vlm.backends.*.probe.timeout_s` (the 0.01 in the test) stays a labelled test-only value; the existing timeout effect and refusal tests (`test_model_production_probes.py`, round 1 audit) still pass. Classes unchanged: `PROBE_*` codes are protocol-bound.

### Cleanup and remaining work

Scratch runner, shim package, basetemps and Hypothesis state removed; `fix2-*.log` kept. No process left, no Git write, install, model call or `.secrets/` access. Remaining: Codex retest and PM full suite. **Lesson:** a changed shared structure (here a new key in `errors`) needs the grep of its readers in files the merge did not change, because no reviewer runs an unchanged test file; the PM's full suite caught it.

## Worker completion (integration fix round 2)

- [x] Changes: stale assertion updated; every other reader of `ProbeResult.errors` listed and unaffected.
- [x] Checks: red reproduced, 10 files green, 3 of 3 mutants killed; Ruff not_run.
- [x] Cleanup: scratch and basetemps removed.
- [x] Remaining: Codex retest, PM full suite.
