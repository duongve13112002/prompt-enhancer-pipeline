# T-016: command-line entry points and the deployment run guide

Status: **Step 0 drafted (Gemini 3.8 Flash scout, 2026-11-18, read against the T-015 post-change files), PM-reviewed; the builder writes its own Step 0 from this draft once T-015 is accepted.**

## Task brief

- **Goal (plan row T-016):** command-line entry points for every command of `docs/execution.md` "Commands" not yet wired, built on the T-015 production coordinator (`src/prompt_enhancer/pipeline.py`, `GoldenAdapter`), and a run guide the user follows on the deployment machine (R-020).
- **Depends on:** T-015 (under review, not yet on main).
- **Requirements:** R-001, R-004, R-009, R-015, R-018, R-020, R-024, R-026, R-027.
- **Out of scope:** model choice, prompts, thresholds or any profile value; live runs (T-017 pilot); the deployment run itself.

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
