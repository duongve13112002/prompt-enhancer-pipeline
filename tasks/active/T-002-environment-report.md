# T-002 Environment report (read-only)

Kind: scout
R-IDs: R-019, R-020
Task file: tasks/active/T-002-environment-report.md

## Brief header

- **Goal:** replace every assumption about the machine with a measured fact, in one report file, so the plan's estimates can be corrected. The agent runs the script on the dev machine; the user runs the same script on the server themselves (R-020) and pastes the output. Measurements that need the GPU or a model are later tasks (the dev smoke E12 and T-031), which need the user's go.
- **Done check:** a read-only script `tools/env_report.py` (stdlib only, runnable on any machine) and its output on the dev machine, `.tmp/T-002/environment-report.md`, which lists, with the exact command and its output for each: CPU core count, RAM, free disk on the intended `data_root`, OS, Python version, whether `pip` works online or only from a wheelhouse, GPU model and count, driver and CUDA version, installed torch and transformers versions, whether a `llama-server` binary is present and its version output, the file-system type of the data and scratch paths (read-only `df -T`), and the CPU and memory limits of the process (cgroup files when present); the report marks each missing item `unknown` with the reason, and any value that needs a GPU or a model is `not_run` with the reason.
- **Out of scope:** benchmarks, model loads, installs, any fix to what the report finds (list it under `Noticed, not done`).
- **Output:** the report file plus a reply under 15 lines.
- **Step budget:** about 25 tool calls.
- **Scope:** may create `tools/env_report.py` and write `.tmp/T-002/`; the script runs read-only commands only (`nvidia-smi` is read-only and allowed once, after checking with the PM that no other session runs a GPU job). No benchmark, no model smoke test and no model load: those are later tasks.
- **Run limit:** under 10 minutes; no downloads, no paid calls, no network, no model load.
- **May assume / Must ask:** may assume nothing about the machine; must ask before any command that is not read-only.
- **Role pack and CLI wrapper:** scout; the CLI is stated by the PM at assignment.
- **Trivial:** no.

## Task brief

- **Task name:** T-002 environment report.
- **Responsible role:** scout (read-only; Step 0 at the top of the report, then continue).
- **Task difficulty and assigned model:** easy; the PM states the model at assignment.
- **Reasoning effort and reason:** medium; commands are simple and output must be copied exactly.
- **Preloaded skills:** `handoff-report`.
- **Current status:** nothing measured; nothing in `docs/serving.md` or `docs/dev-machine.md` is measured.
- **What was already done:** nothing.
- **Expected output:** the report file, a completion report with `not_run` for every skipped item, and a handoff.
- **Constraints and security cautions:** follow standing rule S07 (other sessions share the GPU); never kill another process; stop every process you start; change no environment.
- **What must not be repeated:** guessing a value that a command can show.
- **Ignored evidence paths:** `.tmp/T-002/`, which is Git-ignored and holds the report the verdict depends on.

### Research required before code

1. Which commands report each fact on Windows and on Linux (`nvidia-smi`, `wmic` or `Get-CimInstance`, `lscpu`); verify with `--help` before use.
2. Whether the installed Python, torch and transformers versions can be read without importing heavy libraries (`pip list` or package metadata).

### Configuration impact

None, because this task only reads the machine and writes one report under `.tmp/T-002/`; the intended `data_root` path is an input given by the user and is not stored in any profile by this task.

## Role: scout (read-only)

- Read, search and summarise; change no tracked file.
- Answer with facts and pointers: file and line, or the command and its output. Write `unknown` for what you could not find and say where you looked.
- Keep to the question in the brief. List other things you notice as one line each under `Noticed, not done`.
- For a cold read (R-028): read only what a new agent would read at session start, then report what the project is, what you would do first, what was unclear or contradictory (with file and line), and the questions you would ask the user.
- Return a condensed report (about one page) and file paths, not long excerpts.

# Agent brief footer (append verbatim to every brief)

These rules hold for every worker; a broken one fails the handoff. Rules for your role are in the role pack above. Each rule gives its reason so you can apply it to cases it does not name.

**Step 0 first:** unless the brief marks the task `trivial`, write the template's Step 0 in your task file, reply with it and wait for approval (read-only roles and briefs marked `Small: yes` put it atop the report and continue). Then build step by step, two task-file lines per step.

1. **Environment:** use the project `.venv` only (path in `.agents/project.json`) with `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH` set to the worktree's `python.src_dir`; pytest `-n` at most 2; temp and scratch under `<worktree>/.tmp/<task>/`. Installs, GPU, models, downloads and network calls happen only when the brief allows them, because the machine and quotas are shared.
2. **Owned by the PM:** `.secrets/` stays unopened; commits and pushes belong to the user; other version control writes and the files `tasks/STATUS.md`, `PROJECT_CONTEXT.md`, `IMPLEMENTATION_PLAN.md`, `DECISIONS_INDEX.md` belong to the PM unless the brief names the file and the edit. When a permission or policy denies an action, report it; fix your own malformed input and try again.
3. **Evidence, not assumption:** check library behaviour, defaults and file claims in the installed source or primary docs and cite them; write what you could not check as an assumption. Unclear semantics, permissions or model identity: stop and ask.
4. **Report what happened:** quote the exact output of lint and `ruff format --check`; an unrun test or mutant is `not_run` with the reason; a test double is labelled as one.
5. **Mutants and probes** run on a scratch copy of the trees they need, so the working tree stays clean.
6. **Faulthandler:** in subprocess tests use `-X faulthandler` or `faulthandler.enable()`; bound hangs with test timeouts and report crashes and hangs as findings.
7. **Leave nothing running:** stop every process you started, by its recorded PID, so no other session's process is touched.
8. **Clean up** scratch before handoff, keeping the small reports you cite; remove links as links.
9. **Configuration:** AGENTS.md section 5 and the template's audit.
10. **Test slots:** run every pytest, mutant or probe command through the limiter from the project root: `<venv python> -B tools/agent-workflow/test_slot.py -- <interpreter> -m pytest ...`. It allows 4 test commands at once (limit file `.tmp/pm-state/test-slots/max`, else 2); never bypass it.
11. **Economy:** final reply about 10 short lines with details in the task file (reviewers add the full findings index); keep tool output small (`pytest -q`, grep, tail).
12. **GPU for models:** permitted model work runs on the GPU through the profile's device setting; when the GPU is busy, wait or report, because a CPU fallback gives results that mean something else.
13. **Hooks:** follow `[ctx-nudge]` and `[loop-warning]`.
14. **Recall first:** before asking or deciding, `<venv python> -B -I tools/agent-workflow/recall.py build`, then `recall.py search "<words>"`, so a settled point is not asked again.
15. **Tools:** read with Read, search with Grep or Glob on a scoped path; change tracked files with Edit or Write (shell writes go under `.tmp/`, run logs under `runs/`); put multi-line logic in a script file; check `--help` before using a CLI; make one state change per call and verify it.
16. **Meaning from models, not lists:** source holds no natural-language word list or per-language rule (R-015). Propose the experiment-proven design and name the narrower one you dropped.
17. **Focus:** work toward the brief's Goal and Done check inside Scope. List anything else you notice as one line under `Noticed, not done`. Keep a 2-5 item plan with one item in progress and write a progress line about every 8 tool calls. Stop when the done check passes; when the step budget is spent or the same step fails twice, report what blocks. Return a condensed report and file paths.
