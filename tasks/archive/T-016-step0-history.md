# T-016 Step 0 history (archived verbatim)

Moved from `tasks/active/T-016-cli-run-guide.md` when the phase 1 Completion report replaced the working Step 0 draft. The text below is unchanged: the scout draft (Gemini 3.8 Flash, 2026-11-18) and the PM review of it. The implementer's own Step 0 and the PM answers (decisions 154 and 156) stay in the task file.

## Step 0 draft (scout; PM review below)

### Unknowns and failure modes

- `enhance serve` needs an HTTP server; no web framework is pinned. The standard library (`asyncio` streams or `http.server.ThreadingHTTPServer`, as the T-015 chain test's double uses) avoids a new dependency but needs careful lifecycle code around `Pipeline` (`pipeline.py:77-139`).
- Shutdown must leave nothing running: `pipeline.py:123-136` and `response_lifecycle.py:113-144` drain traces, monitors and disk writes under timeouts; SIGINT or SIGTERM (and Windows console events) must reach that drain.
- `enhance once` returns an envelope plus `attachment_bytes` (`pipeline.py:28-36,182-189`); JSON on stdout cannot carry raw image bytes, so attachments need an output folder.
- `eval build`, `eval run`, `eval fit` have configuration reads (`config.py:82-88`) but no runner module.
- `serving.*` holds `max_concurrency`, `queue_limit`, `total_deadline_s`, `prefix_cache` (`configs/enhancer.example.yaml:403-408`) but no listen address or port.

### Command inventory (scout; served today per `__main__.py:15-40`)

| Command | Served | Calls | Output |
|---|---|---|---|
| `kb sync`, `kb audit` | yes | `kb_sync`, `kb_audit.audit_config` | JSON report |
| `kb status`, `kb rollback`, `kb gc`, `kb show` | no | knowledge store methods | JSON |
| `profile register`, `profile check` | no | `profiles.register_profile`, `profiles.check_profile` with `GoldenAdapter` (`pipeline.py:239`) | JSON |
| `enhance once` | no | `Pipeline.enhance` (`pipeline.py:137-236`) | envelope JSON, attachments to a folder |
| `enhance serve` | no | HTTP around `Pipeline` | HTTP service |
| `eval build`, `eval run`, `eval fit` | no | no runner yet | eval reports |
| embed server | yes (`prompt_enhancer.embed_server`) | | embedding HTTP |

### Ranked questions with proposed answers

1. **HTTP layer for `enhance serve`:** standard library only; `POST /v1/enhance` (the `request.v0` schema), `GET /health`, and an attachment download route; HTTP 503 on queue overflow.
2. **`enhance once` input and output:** `--request <file>` validated against `request.v0` (prompt-only flags optional); pure envelope JSON on stdout; attachments written under a configured or given output folder inside the project (AGENTS.md section 6).
3. **Concurrency and shutdown:** bounded by `serving.max_concurrency` and `serving.queue_limit`; on a stop signal, stop accepting, finish or cancel in-flight requests within the configured deadline, `await pipeline.close()`, exit with nothing left running.
4. **`eval *` scope:** see PM review.
5. **Run guide:** a new `docs/` guide: preflight (VRAM baseline), model servers, `kb sync`, `profile check`, `enhance serve`, smoke requests for generate, edit and retrieval off, teardown with VRAM back at baseline, and a result template the user pastes back.

### Design (scout)

- `__main__.py` extends its subparsers (`kb`, `profile`, `enhance`, `eval`); `--config` and `--overlay` go through `config.load_config(...)` with the T-015 version gate for request commands (`config.py:1808-1812,1959`).
- Exit codes: 0 success, 1 operational failure, 2 configuration or argument refusal (as `tests/test_embed_server_cli.py:94`).
- `enhance serve` runs inside `async with Pipeline(...)` (`pipeline.py:116-121`).

### Configuration impact (scout; existing keys)

`runtime.request_deadline_s`, `runtime.trace.*`, `runtime.monitoring.*` (T-015), `serving.max_concurrency`, `serving.queue_limit`, `serving.total_deadline_s`, `serving.prefix_cache`, `storage.kb_store`, `storage.data_root`, each already validated; T-016 adds only what the PM review below names.

### Plan (scout)

1. Dispatcher and argument validation (`kb status`, `rollback`, `gc`, `show`, `profile register`, `profile check`), with argument property tests.
2. `enhance once` end to end with the labelled HTTP double of `tests/test_pipeline_s0_s6.py` (generate, edit, retrieval on and off).
3. `enhance serve` with concurrency, overflow and shutdown tests (no process left).
4. `eval *` per the PM review.
5. The run guide.

## PM review (2026-11-18)

- **Listen address and port are profile values, not CLI defaults** (AGENTS.md section 5, R-015): add `serving.host` and `serving.port` as validated keys with the sentinel `deploy_required` in the base example and concrete dev values in the dev example; absent or sentinel refused at start. A CLI flag may override only if the design says so explicitly and tests both.
- **`eval run` is needed for the T-017 pilot**, which runs the T-003 scenarios (draft set in `data/eval/draft-2026-11-14/`) end to end and scores them. Proposed: T-016 implements `eval run` as "run every scenario of a named set through `Pipeline` and write one result record per scenario plus a summary", reusing the existing scenario format; scoring rules come from `docs/stages/G-evaluation.md` and are not invented; `eval build` and `eval fit` refuse with exit 2 and a precise message until their tasks (T-003 feature part, T-030). PM decision pending user review.
- Attachment output folder: a validated path under the project root; nothing written outside it.
- Windows and Linux: shutdown on Ctrl+C and on SIGTERM where it exists; tests run on both in CI (R-025).
- Builder: after T-015 acceptance, per decision 91 (T-016 is even: Claude builds, Codex reviews), subject to quota (S32).

## Phase 0 Completion report, Handoff and Worker completion (verbatim, moved from the task file at the phase 1 handoff)

- **Work completed (older, phase 0):** appended only this implementer Step 0; preserved the scout/PM text and pre-existing T-015 patch. No runtime/config/schema/draft-data edits.
- **Result and evidence:** read-only source/artifact checks above. Recall: `<venv-python> -B -I tools/agent-workflow/recall.py build --db .tmp/T-016/recall.sqlite` returned `indexed=94 unchanged=0 removed=0 refused=0 chunks=693 skipped_chunks=0`; searches `T-016`, `evaluation`, `"91"`, `"65"`, `prefix_cache` read before proposing questions. The initial combined query `T-016 eval scoring serving attachments` had no hits. Source/tool limitations: `rg` unavailable; preferred read/grep tools not exposed, and the discovered Node tool was rejected by the role allowlist. Continued with allowed scoped reads; no escalation or policy change attempted.
- **Important changes/reasons:** corrected command/exit-code/serving/shutdown claims, identified the lossy draft projection and missing scoring/provenance contracts, and separated proposals from accepted behavior. New controls and documentation scope changes require PM decisions rather than guessed implementation.
- **Configuration audit:** not applicable to phase 0, because no operational value changed; pre-code impact and planned evidence are above. Every test, lint/format, mutant, live/model/GPU/network run and both-OS execution is **not_run** here (plan-only authorization). Artifact inventory is a read-only source check, not quality/performance evidence.
- **Problems/remaining work:** Q1-Q5 and T-015 acceptance remain open. The whole documented command surface cannot honestly be called implemented while Q4 or scoring are unresolved.
- **Done-check evidence:** `git diff --check -- tasks/active/T-016-cli-run-guide.md` produced no output. `git diff --stat -- tasks/active/T-016-cli-run-guide.md` reported one changed file (the appended plan). Before/after `git diff --binary -- . ':(exclude).secrets' ':(exclude)tasks/active/T-016-cli-run-guide.md'` captures have identical SHA-256 `A3C089C884980DD7F15128E142D217DC7BF8C0B9AA417F4629548CE42775B156`, preserving the other tracked changes. Only this task file was edited outside disposable task scratch; no Git write was performed. Scratch source readers, inventory script, recall database and diff captures are removed before handoff.

### Handoff (phase 0)

- **Current status:** `STEP_0_AWAITING_PM_APPROVAL`. **Already done:** source verification, command inventory, dependency map, conditional design/configuration/test plan. **Next agent:** resolve questions and recheck accepted T-015 APIs, then implement only after the separate approval message. **Must not repeat:** do not reinterpret a draft field from prompt text, promise bounded disk preemption, count the existing skipped-knowledge test as attachment coverage, or make live calls from CPU tests.
- **Worktree/base:** `<project>/.tmp/worktrees/T-016`, main `8d6caf8` plus supplied T-015 patch; no partial runtime patch produced. **PM notes:** record accepted decisions in the PM-owned index/status; Q1-Q5 are still proposals. **Noticed, not done:** existing docs' global statements that nothing is implemented are stale (for example `docs/test-plan.md:3`); only command/run-guide reconciliation belongs in this task. **Lessons:** no new general policy proposed; apply the existing blocking-call, real-entry-point and anchored-write lessons.

### Worker completion (phase 0)

- [x] **Changes:** Appended the implementer's source-verified Step 0 and completion sections to this task file only; no runtime/configuration edits or Git writes. Preserved the supplied T-015 patch.
- [x] **Checks:** Source and read-only draft-artifact verification completed; `git diff --check -- tasks/active/T-016-cli-run-guide.md` passed with no output. Other tracked changes had matching before/after diff hashes, recorded above. Tests, lint/format, mutants and live runs are `not_run` because phase 0 authorizes a plan only.
- [x] **Cleanup:** Removed the owned `.tmp/T-016` readers, inventory script, recall database and diff captures; `Test-Path -LiteralPath .tmp/T-016` returned `False`. No service or model process was started.
- [x] **Remaining:** Await PM approval of Step 0 and Q1-Q5, plus T-015 acceptance. Implementation requires a separate approval message; none has started.
