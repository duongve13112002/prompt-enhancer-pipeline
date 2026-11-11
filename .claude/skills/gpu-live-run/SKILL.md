---
name: gpu-live-run
description: Procedure for any live model or GPU run in this repository (llama-server model on the dev machine, embedding passes, knowledge-base observation passes, latency or throughput measurements) on the dev machine or a GPU server. Covers authorization, preflight, resource ownership, evidence and mandatory teardown.
---

# Live GPU run procedure

The authoritative rules are sections 4, 6, 7 and 8 of `AGENTS.md`, `docs/rules/PLATFORM_GPU_RULES.md` and, for model calls, `docs/rules/VLM_CALL_RULES.md`. A live run is allowed only when the task brief authorizes it; deployment runs are started by the user (the agent prepares the run guide and scripts and reads pasted results).

## Preflight

1. Use only the project interpreter (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on Linux). Set `TEMP`, `TMP`, `PIP_CACHE_DIR`, `HF_HOME` and similar caches to paths under `<project_root>/.tmp` or `.cache` for every process you start.
2. GPU peers (standing rule S07): `nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv` first; if another session uses the GPU, wait and recheck, never kill it; announce "starting GPU job" and "GPU free".
3. Verify the model artifact against its manifest (repository, revision, file, size, SHA-256) without downloading; download only when the brief authorizes it, into the configured project-local path.
4. Never open, print, hash or copy anything under `.secrets/`. No paid or cloud call.
5. When the project has a runtime package, `python tools/agent-workflow/run_approval_gate.py check --run <name> --profile <profile>` must pass: it refuses a resolved profile that differs from the one the user approved.
6. Dev-machine runs use the dev profile (`purpose: smoke_only`, `calibration_allowed: false`): their outputs never enter thresholds, calibration, bake-offs, ground truth or timing claims.

## One job at a time

- Run one job, then STOP and report before the next. Open a sample of real inputs and outputs (the image as sent to the model, the request, the model output) and give a verdict per item: `meets target`, `partial` or `misses`, with the reason.
- Record the image size actually sent to the model; drivers may only scale uniformly or crop.
- Prefer the production entry points over scratch drivers; a driver that produces evidence is code under test.

## During the run

- On a small-VRAM machine only one heavy model is resident at a time; CPU stages keep running in parallel.
- Outputs go below `runs/` (Git-ignored) in a new directory per run, or under the configured `data_root`.
- Read each command's `--help` before use; never invent flags. Sample peak VRAM, RSS and I/O throughput when the harness does not.
- If the host blocks a binary, report it; never change operating-system security settings.

## Teardown (mandatory, also after failure, timeout or cancellation)

1. Stop every server, worker and subprocess you started, by recorded PID; confirm no child remains.
2. Confirm the ports you opened are free.
3. Confirm GPU memory returned to the recorded baseline; cache clearing alone is not proof.

## Evidence to report

- Exact commands, profile path and hash, model identity (repository, revision, file hashes), runtime versions (driver, CUDA, torch, llama.cpp; the VLM backend's model id and version).
- Output directory, report hashes, startup versus steady-state durations, peak VRAM and RSS, throughput, teardown confirmation.
- Label each result: unit, mock_contract, replay, CPU live, GPU live, quality review or performance. HTTP 200 or a schema-valid response proves nothing about quality.
- Evidence produced before a code or dependency change is stale for the changed path; say so.
